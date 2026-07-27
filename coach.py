#!/usr/bin/env python3
"""1부 사용량 코치 — codexbar 사용량을 읽어 '작업 크기 + 이유 + (필요시) 타이밍'을 권고.

명세: docs/logic-design.md, docs/usage-schema.md
출력: 터미널 CLI(색 + 메시지). 리프레쉬(watch) 기본 탑재.
"""
import argparse
import datetime as dt
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import tty
import unicodedata

# ── 튜닝 파라미터 (사용 패턴 따라 조정) ───────────────────────────────
M = 0.10        # 페이스 마진 (left vs timeLeft 비교 여유)
T_BIG = 0.40    # 큰 작업을 돌릴 5h/소진형 잔량 하한
T_SOON = 30     # "초기화 임박" 판정 분(分)
WATCH_INTERVAL = 30   # watch 모드 기본 자동 갱신 주기(초)

# ── 2부 요금가드(--hook) 설정 ─────────────────────────────────────────
# 훅은 임의 cwd에서 실행되므로 고정 절대경로에서 설정을 읽는다(없으면 기본값).
GUARD_CONFIG_PATH = os.path.expanduser("~/.config/usage-coach/guard.json")
# 벤더 무관 런타임 스위치. 존재=on, 부재=off (`coach guard on|off`로 토글).
GUARD_ENABLED_PATH = os.path.expanduser("~/.config/usage-coach/guard-enabled")
GUARD_DEFAULT = {"m": 0.10, "floor": 0.10, "pace": True}   # provider별 기본: 페이스 마진 / 절대 하한 / 페이스 게이트 on
GUARD_TARGETS = ["claude", "codex", "antigravity"]   # 가드 대상(gemini == antigravity)
WEEKLY_MINUTES = 10080   # 7일 윈도우. 가드는 이 윈도우만 본다(5h는 무시).

# 라이브 조회 명령 (provider별 별도 — `--provider all`은 hang)
LIVE_CMDS = {
    "claude": ["codexbar", "usage", "--provider", "claude", "--format", "json"],
    "codex":  ["codexbar", "usage", "--provider", "codex", "--source", "cli", "--format", "json"],
    "antigravity": ["codexbar", "usage", "--provider", "antigravity", "--format", "json"],
}

# --account PROVIDER:LABEL 오버라이드 → codexbar --account 패스스루 (main에서 채움)
ACCOUNT_OVERRIDES = {}

# ANSI 색 (emoji별). tty가 아니거나 NO_COLOR면 비활성.
_COLORS = {"🔴": "31", "🟢": "32", "⏳": "36", "🟡": "33", "⚪": "37"}
# provider별 막대 색 (codexbar 메뉴 느낌)
PROV_COLOR = {"claude": "33", "codex": "36", "antigravity": "35"}
# windowMinutes -> 표시 이름
WIN_NAME = {300: "5시간", 10080: "7일", 1440: "일일"}
# Max 구독 전용 Fable 5 주간 한도의 extraRateWindows id (기본 구독 페이로드엔 없음)
FABLE_WIN_ID = "claude-weekly-scoped-fable"


def _use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def _paint(emoji, text):
    return _c(f"1;{_COLORS.get(emoji, '37')}", text)


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _w(s):
    """문자열의 터미널 셀 폭(ANSI 제외, 한글=2)."""
    s = _ANSI.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, cells):
    return s + " " * max(0, cells - _w(s))


def _row(left, right, width):
    """left는 좌측, right는 우측 끝에 맞춰 정렬(가운데 공백 채움)."""
    return left + " " * max(1, width - _w(left) - _w(right)) + right


def _bar(left_frac, code, n=12):
    fill = round(left_frac * n)
    return _c(code, "█" * fill + "░" * (n - fill))


def _dur(mins):
    """남은 분 -> '4일 2시간' / '3시간 53분' / '12분'."""
    mins = round(mins)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}일 {h}시간" if h else f"{d}일"
    if h:
        return f"{h}시간 {m}분" if m else f"{h}시간"
    return f"{m}분"


def _reset(mins):
    if mins is None:
        return "사용 가능"
    return "곧 초기화" if round(mins) < 1 else f"{_dur(mins)} 남음"


def _term_w():
    return shutil.get_terminal_size((72, 24)).columns


def _wrap(text, width, indent):
    """text를 width 셀 폭에 맞춰 공백 단위로 줄바꿈. 각 줄 앞에 indent 부여."""
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if _w(indent + trial) <= width or not cur:
            cur = trial
        else:
            lines.append(indent + cur)
            cur = word
    if cur:
        lines.append(indent + cur)
    return lines


def windows_summary(usage):
    """표시용 윈도우 목록 -> [(name, leftPct, mins)]. 무효 스킵.

    같은 windowMinutes가 여러 개 오면(예: Claude 주간+Sonnet 주간+Daily, codexbar가
    모델·기능별 하위 한도를 같은 7일로 줌) **가장 빡빡한(left 최소) 1개로 통합**한다.
    제일 적게 남은 게 먼저 막히는 병목 → render·to_payload·classify(pick)가 같은
    윈도우를 보게 해 중복 표시·불일치를 없앤다. (동일값 중복도 자연히 1개로 묶임.)"""
    best, order = {}, []   # wm -> 가장 빡빡한 r, wm 첫 등장 순서
    for k in ("primary", "secondary", "tertiary"):
        r = win(usage.get(k))
        if not r:
            continue
        wm = r["wm"]
        if wm not in best:
            order.append(wm)
            best[wm] = r
        elif r["left"] < best[wm]["left"]:
            best[wm] = r
    return [(WIN_NAME.get(wm, f"{wm}분"), round(best[wm]["left"] * 100), best[wm]["mins"])
            for wm in order]


def fable_window(usage):
    """extraRateWindows의 Fable 5 전용 주간 한도 -> (name, leftPct, mins) or None.

    표시 전용 — classify(pick)·guard 판정에는 넣지 않는다(전체 주간 기준 유지).
    기본 구독 페이로드엔 항목 자체가 없다 → None(라인 조용히 생략). id로만 식별해
    Daily Routines 등 다른 extra 윈도우를 오인하지 않는다. 무효 윈도우는 win()이 스킵.
    """
    for e in usage.get("extraRateWindows") or []:
        if isinstance(e, dict) and e.get("id") == FABLE_WIN_ID:
            r = win(e.get("window"))
            if r:
                return ("Fable", round(r["left"] * 100), r["mins"])
    return None


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def win(w):
    """윈도우 dict -> {left, timeLeft, mins, wm} or None(무효: 필드 누락/epoch/파싱불가).

    codexbar가 windowMinutes/usedPercent/resetsAt를 null·누락으로 줄 때가 있어
    (예: antigravity 간헐적) 그 윈도우는 스킵해 provider 전체 크래시를 막는다.
    """
    if not w:
        return None
    wm, up, rs = w.get("windowMinutes"), w.get("usedPercent"), w.get("resetsAt")
    if not wm or up is None:   # 누락/null/0 → 스킵
        return None
    if not rs:
        # codexbar may omit resetsAt when a short window is completely full.
        # Treat usedPercent=0 as a valid "100% available" window.
        if up == 0:
            return {"left": 1.0, "timeLeft": 1.0, "mins": None, "wm": wm}
        return None
    try:
        resets = dt.datetime.fromisoformat(rs.replace("Z", "+00:00"))
    except Exception:
        return None
    if resets.year < 2000:   # epoch/placeholder 가드
        return None
    mins = max(0.0, (resets - now_utc()).total_seconds() / 60.0)
    left = max(0.0, min(1.0, (100 - up) / 100.0))   # 범위 밖 입력 가드
    return {"left": left, "timeLeft": mins / wm, "mins": mins, "wm": wm}


def pick(usage, minutes):
    """windowMinutes로 윈도우 선택 — 같은 wm 여러 개면 가장 빡빡한(left 최소) 1개.
    windows_summary와 같은 기준이라 render·json·classify가 동일 윈도우를 본다."""
    best = None
    for k in ("primary", "secondary", "tertiary"):
        r = win(usage.get(k))
        if r and r["wm"] == minutes and (best is None or r["left"] < best["left"]):
            best = r
    return best


def classify(provider, usage):
    """logic-design.md 출력 5종 분류. -> (emoji, action, detail).
    action = 권장 작업 크기(헤드라인), detail = 이유 + (필요시) 타이밍. 코치 3원칙 유지.

    판정 순서(먼저 걸리는 것):
      #1 🔴 7일(페이스형) 빠듯  → 주간 한도가 병목
      #2 🟢 소진형(5h/일일) 넉넉(left>=T_BIG) → 큰 작업 기회
      #3 ⏳ 소진형 부족 + 초기화 임박(mins<=T_SOON) → 잠깐 대기
      #4 🟡 소진형 부족 + 페이스 뒤처짐(left<timeLeft-M) → 작은 작업만
      #5 ⚪ 그 외(페이스에 맞게 사용 중) → 평소대로
    충돌 해소: 5h·7d 둘 중 빡빡한 쪽이 상한 → 7일 빠듯(#1)이 5h 여유보다 우선.
    예외: 소진형 윈도우가 아예 없는 페이스 전용 provider(Antigravity=주간만)는
          7일 페이스만으로 🔴/🟢/⚪ 코칭(5h+7d 본 로직은 불변).
    """
    seven = pick(usage, 10080)             # 7일(페이스형)
    short = pick(usage, 300) or pick(usage, 1440)   # 소진형: 5h 또는 일일

    # 소진형이 없는 페이스 전용 provider(예: Antigravity = 주간만) → 7일 페이스로 코칭
    if not short:
        if not seven:
            return (None, "데이터 부족", "표시할 한도 윈도우가 없어요.")
        wp = round(seven["left"] * 100)
        if seven["left"] < seven["timeLeft"] - M:
            return ("🔴", "큰 작업은 미루세요",
                    f"주간 한도가 빠듯해서(남은 {wp}%) 지금 크게 쓰면 주 후반에 막혀요. "
                    f"꼭 필요한 것만 돌리세요.")
        if seven["left"] >= T_BIG:
            return ("🟢", "큰 작업 돌려도 돼요",
                    f"주간 한도가 {wp}% 남아 페이스에 여유 있어요. 평소보다 크게 써도 괜찮아요.")
        return ("⚪", "평소대로 쓰세요",
                f"주간 한도 {wp}% 남았고 페이스에 맞게 쓰는 중이라 여유 있어요.")

    sp = round(short["left"] * 100)

    # #1 — 7일 한도가 병목 (페이스형 있을 때만 판정 → Gemini 자동 스킵)
    if seven and seven["left"] < seven["timeLeft"] - M:
        return ("🔴", "큰 작업은 미루세요",
                f"7일 한도가 가장 빠듯해서(남은 {round(seven['left']*100)}%) 지금 크게 쓰면 "
                f"주 후반에 막혀요. 꼭 필요한 작은 것만 돌리세요.")

    # #2 — 소진형 넉넉 → 큰 작업 기회
    if short["left"] >= T_BIG:
        long_ok = " 장기도 여유라" if seven else ""
        return ("🟢", "지금 큰 작업 돌리세요",
                f"단기 한도 {sp}% 남고{long_ok} 끊길 걱정 없어요. "
                f"남기면 초기화로 사라지니 지금이 적기예요.")

    # #3 — 소진형 부족 + 초기화 임박 → 잠깐 대기
    if short["mins"] <= T_SOON:
        return ("⏳", "조금만 기다렸다 큰 작업",
                f"단기 한도가 {sp}%뿐이지만 약 {_dur(short['mins'])} 뒤 "
                f"초기화되면 풀로 쓸 수 있어요.")

    # #4 — 소진형 부족 + 페이스 뒤처짐 → 작은 작업만
    if short["left"] < short["timeLeft"] - M:
        return ("🟡", "작은 작업으로 돌리세요",
                f"단기 한도가 {sp}%라 큰 작업은 중간에 끊겨요. "
                f"초기화까지 {_dur(short['mins'])} 남았어요.")

    # #5 — 그 외(페이스에 맞게 사용 중)
    return ("⚪", "평소대로 쓰세요",
            f"단기·장기 모두 페이스에 맞게(단기 {sp}%) 쓰는 중이라 여유 있어요. "
            f"아주 큰 작업만 한 번 더 확인하세요.")


# ── 입력 소스 ─────────────────────────────────────────────────────────
def antigravity_gemini_only(usage):
    """antigravity 2풀(Gemini=primary / Claude·GPT=secondary) 중 Gemini만 남긴다
    — Claude·GPT 풀은 미사용이라 표시하면 소음. windowMinutes가 간헐적으로 빠진 채
    오면 주간(10080)으로 보정한다(Antigravity 풀은 전부 Weekly Limit)."""
    u = dict(usage)
    p = u.get("primary")
    if isinstance(p, dict) and p.get("usedPercent") is not None:
        p = dict(p)
        p.setdefault("windowMinutes", 10080)
    u["primary"], u["secondary"], u["tertiary"] = p, None, None
    return u


def fetch_live(provider):
    """codexbar 라이브 조회 -> usage dict. 실패 시 예외.

    start_new_session=True: codexbar가 우리 제어터미널/포그라운드 그룹을 가로채지
    못하게 별도 세션으로 실행 → Ctrl-C(SIGINT)가 codexbar에 먹히지 않고 watch가 정상 종료.
    stdin=DEVNULL: codexbar가 우리 입력을 읽지 않도록.
    """
    cmd = list(LIVE_CMDS[provider])
    if provider in ACCOUNT_OVERRIDES:
        cmd += ["--account", ACCOUNT_OVERRIDES[provider]]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=40, start_new_session=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "조회 실패").strip().splitlines()[-1])
    data = json.loads(out.stdout)
    usage = data[0]["usage"]
    return antigravity_gemini_only(usage) if provider == "antigravity" else usage


def fetch_live_all(provider):
    """codexbar --all-accounts -> 등록된 전 계정의 usage dict 리스트."""
    cmd = list(LIVE_CMDS[provider]) + ["--all-accounts"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=120, start_new_session=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "조회 실패").strip().splitlines()[-1])
    usages = [e.get("usage") or {} for e in json.loads(out.stdout)]
    if provider == "antigravity":
        usages = [antigravity_gemini_only(u) for u in usages]
    return usages


def load_file(path):
    with open(path) as f:
        return json.load(f)[0]["usage"]


def _mock_window(left, mins, wm):
    resets = (now_utc() + dt.timedelta(minutes=mins)).isoformat().replace("+00:00", "Z")
    return {"usedPercent": round((1 - left) * 100), "windowMinutes": wm, "resetsAt": resets}


# 출력 5종을 의도적으로 띄우는 모킹 입력 (영상 촬영용 — 실제 한도를 다 못 칠 때)
def mock_usage(scenario):
    """scenario '1'~'5' -> 해당 출력이 나오도록 구성한 usage dict."""
    ok7 = _mock_window(0.80, 5040, 10080)   # 7일 여유 (left .8 > timeLeft .5)
    table = {
        # five(left,mins), seven
        "1": (_mock_window(0.60, 200, 300),  _mock_window(0.20, 6048, 10080)),  # 🔴 7일 병목
        "2": (_mock_window(0.70, 200, 300),  ok7),                              # 🟢 큰 작업 기회
        "3": (_mock_window(0.20, 10,  300),  ok7),                              # ⏳ 초기화 임박
        "4": (_mock_window(0.10, 180, 300),  ok7),                              # 🟡 페이스 뒤처짐
        "5": (_mock_window(0.30, 75,  300),  ok7),                              # ⚪ 평소대로
    }
    five, seven = table[scenario]
    return {"primary": five, "secondary": seven, "tertiary": None}


# ── 렌더링 ────────────────────────────────────────────────────────────
def render(results):
    """results: list[dict] -> 출력 문자열. provider별 기본 사용량 + 한 줄 권고(정렬·구분선)."""
    W = max(46, min(_term_w() - 1, 62))
    div = _c("90", " " + "─" * (W - 1))
    out = [_row(_c("1", " 사용량 코치"), _c("90", now_utc().astimezone().strftime("%H:%M")), W), ""]

    blocks = []
    for r in results:
        name = r["label"].title()
        if not r["ok"]:
            blocks.append([f" ⚠️  {name}  {r['msg']}"])
            continue
        code = PROV_COLOR.get(r["label"], "37")
        meta = "  ·  ".join(x for x in (r.get("email"), r.get("plan")) if x)
        b = [_row(_c(f"1;{code}", f" {name}"), _c("90", meta), W)]
        wpad = max([5] + [_w(n) for n, _, _ in r["windows"]])
        for wname, pct, mins in r["windows"]:
            if pct is None:            # 계정 행에서 미확인(윈도우 데이터 없음)
                left = f"   {_pad(wname, wpad)} {_bar(0, code)}    —"
                b.append(_row(left, _c("90", "미확인"), W))
                continue
            left = f"   {_pad(wname, wpad)} {_bar(pct / 100, code)}  {pct:>3}%"
            b.append(_row(left, _c("90", _reset(mins)), W))
        if r["emoji"]:
            b.append("   " + _paint(r["emoji"], f"{r['emoji']} {r['action']}"))
            b += _wrap(r["detail"], W, "      ")   # 이유·타이밍: 헤드라인 아래 정렬 줄바꿈
        elif not r["windows"]:                     # 코칭 불가(윈도우 데이터 없음/불완전)
            b.append(_c("90", f"   · {r['detail']}"))
        blocks.append(b)

    for i, b in enumerate(blocks):
        out += b
        out.append(div if i < len(blocks) - 1 else "")

    # 전체 종합 한 줄 — 가장 빡빡한(우선순위 높은) 케이스
    order = {"🔴": 0, "🟡": 1, "⏳": 2, "⚪": 3, "🟢": 4}
    valid = [(r["emoji"], r["label"].title()) for r in results if r["ok"] and r["emoji"]]
    if valid:
        we, wl = min(valid, key=lambda x: order.get(x[0], 9))
        verdict = {"🔴": "주간 한도부터 챙기세요", "🟡": "큰 작업은 미루세요",
                   "⏳": "잠깐 기다리면 풀로 가능", "⚪": "평소대로", "🟢": "큰 작업 OK"}[we]
        out.append(_paint(we, f" 종합  {we} {wl} — {verdict}"))
    return "\n".join(out)


# emoji -> 안정적 level 문자열 / 윈도우 이름 -> JSON 키 (agent loop이 분기에 사용)
_LEVEL = {"🔴": "red", "🟢": "green", "⏳": "wait", "🟡": "yellow", "⚪": "white"}
_WKEY = {"5시간": "5h", "7일": "7d", "일일": "daily", "Fable": "fable_7d", "Gemini": "gemini"}


def to_payload(results):
    """agent loop용 머신 출력. 코칭 정보(action·reason 전문)를 손실 없이 담는다.
    판단은 하지 않음(provider별 raw 정보만) — gate/종합 없음."""
    provs = {}
    for r in results:
        if not r["ok"]:
            provs[r["label"]] = {"ok": False, "error": r["msg"]}
            continue
        provs[r["label"]] = {
            "ok": True,
            "plan": r.get("plan"),
            "email": r.get("email"),
            "level": _LEVEL.get(r["emoji"]),     # 코칭 불가 시 null
            "action": r["action"],               # 권장 작업 크기 (전문)
            "reason": r["detail"],               # 이유·타이밍 (전문, 그대로)
            "windows": {_WKEY.get(n, n): {"left_pct": p,
                                          "reset_min": None if m is None else round(m)}
                        for n, p, m in r["windows"]},
        }
    return {"ts": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "providers": provs}


def load_usage(kind, arg):
    """kind(live|live-all|file|mock) + arg -> usage dict. 실패 시 예외(호출부에서 처리).
    live-all은 guard 등 단일 usage가 필요한 호출부에선 선택 계정 단일 조회로 축약."""
    if kind in ("live", "live-all"):
        return fetch_live(arg)
    if kind == "file":
        return load_file(arg)
    return mock_usage(arg)


def gather_all_accounts(label):
    """전 계정을 한 블록으로: 행 = 계정(라벨 = 이메일 @ 앞부분), 코칭 = 최여유 계정 기준."""
    order = {"🔴": 0, "🟡": 1, "⏳": 2, "⚪": 3, "🟢": 4}
    accounts = []
    for usage in fetch_live_all(label):
        emoji, action, detail = classify(label, usage)
        wins = windows_summary(usage)
        pct, mins = (wins[0][1], wins[0][2]) if wins else (None, None)
        accounts.append({"email": usage.get("accountEmail") or "?",
                         "plan": usage.get("loginMethod"), "pct": pct, "mins": mins,
                         "emoji": emoji, "action": action, "detail": detail})
    if not accounts:
        return {"label": label, "ok": False, "msg": "등록된 계정 없음"}
    accounts.sort(key=lambda a: (-order.get(a["emoji"], -1),
                                 -(a["pct"] if a["pct"] is not None else -1)))
    best = accounts[0]
    return {
        "label": label, "ok": True,
        "email": best["email"], "plan": best["plan"],
        "windows": [(a["email"].split("@", 1)[0], a["pct"], a["mins"]) for a in accounts],
        "emoji": best["emoji"], "action": best["action"], "detail": best["detail"],
    }


def gather_one(source):
    """source(label, kind, arg) -> result dict 1개."""
    label, kind, arg = source
    try:
        if kind == "live-all":
            return gather_all_accounts(label)
        usage = load_usage(kind, arg)
        emoji, action, detail = classify(label, usage)
        wins = windows_summary(usage)
        if label == "antigravity":   # 2풀 중 Gemini만 표시 — 항목명도 실물대로
            wins = [("Gemini", pv, mv) for _n, pv, mv in wins]
        fable = fable_window(usage)
        if fable:
            wins.append(fable)     # 표시 전용 추가 — classify에는 미반영
        return {
            "label": label, "ok": True,
            "email": usage.get("accountEmail"),
            "plan": usage.get("loginMethod"),
            "windows": wins,
            "emoji": emoji, "action": action, "detail": detail,
        }
    except subprocess.TimeoutExpired:
        return {"label": label, "ok": False, "msg": "조회 시간초과(hang?) — 건너뜀"}
    except Exception as e:
        return {"label": label, "ok": False, "msg": f"조회 실패: {e}"}


def gather(sources):
    """sources: list[(label, kind, arg)] -> results(dict) 리스트. kind: live|live-all|file|mock.

    순차 조회 고정 — codexbar는 동시 실행하면 잠금/행이 걸린다(실측: 병렬 2 인스턴스에서
    hang·조회 실패, `--provider all` hang과 같은 계열). provider당 수십 초라 전체
    45초~1분쯤 걸리지만 병렬화로 줄일 수 없는 codexbar 쪽 제약이다."""
    return [gather_one(s) for s in sources]


# ── 2부 요금가드 (command 훅 결정 모드) ───────────────────────────────
def load_guard_config(path=GUARD_CONFIG_PATH):
    """guard.json -> {provider: {m, floor, pace}}. 파일/키 누락은 GUARD_DEFAULT로 보완.

    훅은 임의 cwd에서 돌아 상대경로가 안 통하므로 고정 절대경로에서 읽는다.
    파싱 실패·파일 없음도 조용히 기본값 — 설정 오류로 작업을 죽이지 않는다(fail-open).
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):    # [] 등 dict 아닌 JSON → 기본값(fail-open)
        raw = {}

    def _num(entry, key):
        try:
            return float(entry.get(key, GUARD_DEFAULT[key]))
        except (TypeError, ValueError):   # "bad"/null 등 숫자 아닌 값 → 기본값
            return GUARD_DEFAULT[key]

    def _pace(entry):
        v = entry.get("pace", GUARD_DEFAULT["pace"])
        return v if isinstance(v, bool) else GUARD_DEFAULT["pace"]   # 깨진 값 → 기본 true

    cfg = {}
    for prov in GUARD_TARGETS:
        entry = raw.get(prov)
        if entry is None and prov == "antigravity":   # gemini 별칭 허용
            entry = raw.get("gemini")
        if not isinstance(entry, dict):    # 누락/null/스칼라 → 기본값
            entry = {}
        cfg[prov] = {"m": _num(entry, "m"), "floor": _num(entry, "floor"), "pace": _pace(entry)}
    return cfg


def guard_check(sources, cfg):
    """7일 윈도우 하이브리드 OR 가드. -> stopReason(str) 또는 None(통과).

    정지 조건(provider별, 하나라도 걸리면 정지):
      left < timeLeft − m   (pace: 너무 빨리 태움 — pace:false면 건너뜀)
      OR left < floor       (absolute: 바닥 임박)
    fail-open: 조회 실패 / 7일 windowMinutes 없음 → 그 provider는 정지에 기여 안 함.
    """
    triggers = []
    for label, kind, arg in sources:
        try:
            usage = load_usage(kind, arg)
            w = pick(usage, WEEKLY_MINUTES)     # 7일만 — 5h는 보지 않음
        except Exception:
            continue                            # 조회 실패 → fail-open
        if not w:
            continue                            # 7일 윈도우 없음/무효 → fail-open
        c = cfg.get(label, GUARD_DEFAULT)
        m, floor, pace = c["m"], c["floor"], c.get("pace", True)
        # 7일 윈도우 리셋은 최대 7일 후 → timeLeft ≤ 1.0. 손상된 먼 미래 resetsAt가
        # 폭주시켜 엉뚱하게 정지시키지 않도록 클램프(fail-open 철학).
        left, time_left, mins = w["left"], min(1.0, w["timeLeft"]), w["mins"]
        # pace:false = "남은 쿼터 소진" — 페이스 조건을 건너뛰고 floor만 본다.
        if pace and left < time_left - m:
            triggers.append(f"{label} 주간 페이스 초과 — 남은 {left*100:.0f}% "
                            f"< 기준 {(time_left - m)*100:.0f}% (리셋 {_dur(mins)} 후)")
        elif left < floor:
            triggers.append(f"{label} 주간 한도 바닥 임박 — 남은 {left*100:.0f}% "
                            f"< 하한 {floor*100:.0f}%")
    if not triggers:
        return None
    return "요금가드: " + " · ".join(triggers) + " → 작업을 멈추고 확인하세요."


def read_hook_event():
    """하니스가 stdin으로 주는 이벤트 JSON 소비(broken pipe 방지). 내용은 쓰지 않음.

    tty면 블로킹 read를 피하려고 건너뛴다(대화형 수동 테스트 안전).
    """
    if sys.stdin.isatty():
        return {}
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def guard_enabled():
    """런타임 스위치 상태. 플래그 파일 존재=on / 부재=off."""
    return os.path.exists(GUARD_ENABLED_PATH)


def set_guard(on):
    """런타임 스위치 토글. on→플래그 파일 생성(부모 mkdir 포함) / off→삭제.

    설정 디렉토리 권한 오류 등으로 작업을 죽이지 않도록 fail-open(조용히 무시).
    """
    try:
        if on:
            os.makedirs(os.path.dirname(GUARD_ENABLED_PATH), exist_ok=True)
            open(GUARD_ENABLED_PATH, "a").close()   # 빈 플래그 파일 touch
        else:
            try:
                os.remove(GUARD_ENABLED_PATH)
            except FileNotFoundError:
                pass                                # 이미 off
    except Exception:
        pass


def run_guard_cmd(action, sources):
    """`coach guard <on|off|status>` — 벤더 무관 단일 런타임 스위치.

    on/off는 플래그 파일을 토글하고, status는 현재 상태 + 지금 사용량으로 판정 미리보기.
    """
    if action == "on":
        set_guard(True)
        print("요금가드: on (플래그 생성)")
    elif action == "off":
        set_guard(False)
        print("요금가드: off (플래그 삭제)")
    else:   # status (기본)
        state = "on" if guard_enabled() else "off"
        print(f"요금가드: {state}")
        # 지금이면 어떻게 판정되는지 라이브 미리보기(스위치 상태와 무관하게 항상 보여줌).
        try:
            reason = guard_check(sources, load_guard_config())
        except Exception:
            reason = None                           # 조회 오류 → 미리보기 생략(fail-open)
        if reason:
            print(f"  지금이면 정지: {reason}")
        else:
            print("  지금이면 통과")


def run_hook(sources):
    """command Stop 훅 결정 모드(Claude 인-훅). 정지면 {continue:false,...} 1줄, 통과면 무출력.

    게이트 2개를 통과해야 기존 가드 판정 진행(항상 exit 0):
      1) guard-enabled 플래그 부재 → 즉시 통과
      2) stdin 이벤트의 stop_hook_active가 truthy 아님 → 통과(/goal 루프 턴2+에서만 발동)
    """
    if not guard_enabled():
        return                                      # 스위치 off → 통과(무출력)
    event = read_hook_event()                       # stdin 이벤트 소비 + 값 사용
    if not event.get("stop_hook_active"):
        return                                      # 루프 턴 아님 → 통과(무출력)
    reason = guard_check(sources, load_guard_config())
    if reason:
        print(json.dumps({"continue": False, "stopReason": reason}, ensure_ascii=False))
    # 통과면 아무것도 출력하지 않는다.


def run_guard_check(sources):
    """`coach --guard-check` — Codex 워처용 provider-중립 모드. -> exit code(int).

    루프 게이트 없음 + guard-enabled 게이트는 적용(off면 통과).
    출력 계약: 통과/판정불가 → exit 0 + 무출력 / 정지 → exit≠0(10) + 사유 1줄 stdout.
    """
    if not guard_enabled():
        return 0                                    # 스위치 off → 통과(무출력)
    reason = guard_check(sources, load_guard_config())
    if reason:
        print(reason)
        return 10
    return 0


def _draw(text):
    """깜빡임 없이 제자리 갱신: 커서 홈 → 줄마다 EOL까지 지우며 출력 → 아래 잔여 제거."""
    body = "".join(line + "\033[K\n" for line in text.split("\n"))
    sys.stdout.write("\033[H" + body + "\033[J")
    sys.stdout.flush()


def run_watch(sources, interval):
    """watch 모드: interval초마다 자동 갱신 + Enter로 수동 즉시 갱신, q/Ctrl-C로 종료.

    데이터 조회(gather)는 이전 화면이 그대로 떠 있는 상태에서 진행하고,
    새 데이터가 준비된 뒤에만 제자리 갱신 → 갱신 때 화면이 사라지지 않음.
    """
    is_tty = sys.stdin.isatty()
    footer = (f"\n  [{interval}초마다 자동 갱신 · Enter=지금 갱신 · q/Ctrl-C=종료]" if is_tty
              else f"\n  [{interval}초마다 자동 갱신 · Enter=지금 갱신 · q+Enter/Ctrl-C=종료]")
    sys.stdout.write("\033[2J\033[H  사용량 불러오는 중... "
                     "(codexbar를 provider별로 순차 조회해서 1분쯤 걸릴 수 있어요)\033[J")
    sys.stdout.flush()
    old_attr = None
    if is_tty:
        # cbreak + 에코 off: Enter가 화면에 개행을 찍지 않고 바로 갱신 트리거가 된다
        # (기존엔 canonical 모드라 에코된 개행이 커서만 내리고, 갱신 표시도 없어
        #  동작 안 하는 것처럼 보였음). q는 Enter 없이 즉시 종료.
        old_attr = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    try:
        while True:
            frame = render(gather(sources)) + footer   # 느린 조회 동안 이전 프레임 유지
            _draw(frame)
            r, _, _ = select.select([sys.stdin], [], [], interval)
            if r:
                if is_tty:
                    keys = os.read(sys.stdin.fileno(), 1024)
                    if b"q" in keys.lower():
                        break
                elif sys.stdin.readline().strip().lower() == "q":
                    break
            _draw(frame + _c("90", "  갱신 중..."))   # 조회 시작 즉시 진행 표시
    except KeyboardInterrupt:
        pass
    finally:
        if old_attr is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attr)
    print("\n종료합니다.")


def _watch_sec(v):
    try:
        iv = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("주기는 정수(초)여야 해요")
    if iv < 1:
        raise argparse.ArgumentTypeError("주기는 1초 이상이어야 해요")
    return iv


def build_sources(args, p):
    """args(--mock/--file/--providers) -> sources: list[(label, kind, arg)]. kind: mock|file|live.

    기존 main의 소스 결정 로직을 그대로 추출(동작 보존). 형식 오류는 p.error로 종료.
    """
    if args.mock:
        scenarios = ["1", "2", "3", "4", "5"] if args.mock == "all" else [args.mock]
        return [(f"mock#{s}", "mock", s) for s in scenarios]
    if args.file:
        sources = []
        for spec in args.file:
            if ":" not in spec:
                p.error(f"--file 형식은 PATH:PROVIDER 예요 (받은 값: {spec!r})")
            path, prov = spec.rsplit(":", 1)
            sources.append((prov, "file", path))
        return sources
    return [(prov,
             "live-all" if prov == "antigravity" and prov not in ACCOUNT_OVERRIDES else "live",
             prov)
            for prov in args.providers.split(",") if prov.strip()]


def main():
    p = argparse.ArgumentParser(description="codexbar 사용량 코치 — 작업 크기 권고")
    p.add_argument("cmd", nargs="?", choices=["guard"],
                   help="서브커맨드. 'guard' = 요금가드 런타임 스위치")
    p.add_argument("action", nargs="?", choices=["on", "off", "status"],
                   help="guard 액션: on|off|status (생략 시 status)")
    p.add_argument("--providers", default="claude,codex,antigravity",
                   help="라이브 조회할 provider (쉼표구분). 기본 전체")
    p.add_argument("--mock", metavar="N",
                   help="모킹 출력. '1'~'5' 또는 'all'(5종 전부) — 영상 촬영/검증용")
    p.add_argument("--file", action="append", metavar="PATH:PROVIDER", default=[],
                   help="JSON 파일에서 읽기 (예: samples/claude.json:claude). 반복 가능")
    p.add_argument("--account", action="append", metavar="PROVIDER:LABEL", default=[],
                   help="provider 계정 지정(codexbar --account 패스스루, 예: "
                        "antigravity:foo@gmail.com). 반복 가능")
    p.add_argument("--watch", nargs="?", const=WATCH_INTERVAL, type=_watch_sec, metavar="SEC",
                   help=f"watch 모드(주기 자동 갱신, 기본 {WATCH_INTERVAL}초). Enter=수동 갱신")
    p.add_argument("--once", action="store_true",
                   help="한 번만 찍고 종료(watch 안 함). 기본은 watch 모드")
    p.add_argument("--json", action="store_true",
                   help="agent loop용 머신 출력(JSON 1줄, 단발). 코칭 정보 전문 포함, 색·장식 없음")
    p.add_argument("--hook", action="store_true",
                   help="command Stop 훅 결정 모드(7일 가드). 정지면 {continue:false} 출력, 통과면 무출력")
    p.add_argument("--guard-check", action="store_true", dest="guard_check",
                   help="Codex 워처용 가드 체크(provider-중립). 정지면 exit≠0+사유, 통과면 exit0 무출력")
    args = p.parse_args()

    for spec in args.account:
        if ":" not in spec:
            p.error(f"--account 형식은 PROVIDER:LABEL 이에요 (받은 값: {spec!r})")
        prov, label = spec.split(":", 1)
        ACCOUNT_OVERRIDES[prov] = label

    sources = build_sources(args, p)

    # guard 서브커맨드: 런타임 스위치 on/off/status (단발).
    if args.cmd == "guard":
        run_guard_cmd(args.action, sources)
    # --hook: command Stop 훅 결정 모드(단발). 정지면 JSON 1줄, 통과면 무출력.
    elif args.hook:
        run_hook(sources)
    # --guard-check: 워처용 단발. 정지면 exit≠0+사유, 통과면 exit0 무출력.
    elif args.guard_check:
        sys.exit(run_guard_check(sources))
    # --json: agent loop용 머신 출력(단발). 그 외 기본 = watch, --once면 단발 사람용.
    elif args.json:
        print(json.dumps(to_payload(gather(sources)), ensure_ascii=False, separators=(",", ":")))
    elif args.once:
        print(render(gather(sources)))
    else:
        run_watch(sources, args.watch if args.watch is not None else WATCH_INTERVAL)


if __name__ == "__main__":
    main()
