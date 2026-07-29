#!/usr/bin/env python3
"""usage-coach Discord 대시보드 — coach --json 출력을 Components V2 메시지
(provider별 컨테이너 + 유니코드 게이지 바)로 조립해 디스코드 웹훅 메시지 하나를
계속 편집(업서트)한다. level이 나빠지면 새 메시지로 핑.

실행: LaunchAgent가 주기 실행(기본 5분). 수동 검증은 --out 으로 JSON만.
설정: ~/.config/usage-coach/discord.json {"webhook_url": "..."} 또는 env DISCORD_WEBHOOK_URL
상태: ~/.config/usage-coach/discord-state.json {"message_id", "last_levels"}

전부 네이티브 텍스트라 데스크톱·모바일 어느 폭에서도 선명하다(이전 PNG 카드는
세로가 길수록 통째로 축소돼 데스크톱에서 깨알이 됐다). 웹훅으로 components를
보낼 때는 URL에 ?with_components=true 가 없으면 필드가 무시된다(50006).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import coach as coachmod   # 판정 정책 재사용(classify 등) — 정본은 coach

COACH = Path(__file__).with_name("coach.py")
CONFIG_PATH = os.path.expanduser("~/.config/usage-coach/discord.json")
STATE_PATH = os.path.expanduser("~/.config/usage-coach/discord-state.json")
SESSIONS_DIR = os.path.expanduser("~/.config/usage-coach/sessions")
USERNAME = "dashboard"
FLAG_COMPONENTS_V2 = 32768   # 1 << 15

# level -> (색, 이모지, 종합 한 줄) — coach.py의 order/verdict와 동일 축
LEVELS = {
    "red":    ("#F04747", "🔴", "주간 한도부터 챙기세요"),
    "yellow": ("#FAA61A", "🟡", "큰 작업은 미루세요"),
    "wait":   ("#7C8AFF", "⏳", "잠깐 기다리면 풀로 가능"),
    "white":  ("#9AA4B2", "⚪", "평소대로"),
    "green":  ("#43B581", "🟢", "큰 작업 OK"),
}
SEVERITY = {"red": 0, "yellow": 1, "wait": 2, "white": 3, "green": 4}   # 낮을수록 나쁨
# 게이지 라벨 — 코드 스팬 안에서 바와 정렬돼야 하므로 ASCII만(한글은 클라이언트별
# 고정폭 폭이 달라 줄이 어긋난다)
WIN_CODE = {"5h": "5h", "7d": "7d", "daily": "1d", "fable_7d": "Fable", "gemini": "Gemini"}
# provider 브랜드색 — 평상시 컨테이너 accent. 위험(red/yellow/wait)이면 level 색이 덮는다
PROV_HEX = {"claude": "#E5B567", "codex": "#7ED5F5", "antigravity": "#C89BF0"}
# 헤더 점도 같은 규칙 — 이모지 팔레트 한계 내의 브랜드 근사색
PROV_EMOJI = {"claude": "🟠", "codex": "🔵", "antigravity": "🟣"}


# ---------------------------------------------------------------- coach 호출

def fetch_payload(extra_args):
    cmd = [sys.executable, str(COACH), "--json", "--once"] + extra_args
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"coach --json 실패 (exit {out.returncode}): {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def provider_order(provs):
    order = [p for p in ("claude", "codex", "antigravity") if p in provs]
    return order + [p for p in provs if p not in order]


def fetch_all_accounts(provider):
    """전 계정을 coach 정책으로 계정별 판정(antigravity는 coach가 Gemini 풀만 남김).
    여유 있는 순(level 좋은 순 → 잔량 큰 순)으로 정렬해 반환."""
    accounts = []
    for usage in coachmod.fetch_live_all(provider):
        emoji, action, detail = coachmod.classify(provider, usage)
        wins = coachmod.windows_summary(usage)
        if provider == "antigravity":
            wins = [("Gemini", pv, mv) for _n, pv, mv in wins]
        fable = coachmod.fable_window(usage)
        if fable:
            wins.append(fable)
        accounts.append({
            "email": usage.get("accountEmail") or "?",
            "level": coachmod._LEVEL.get(emoji),
            "action": action,
            "reason": detail,
            "windows": {coachmod._WKEY.get(n, n): {"left_pct": p,
                                                   "reset_min": None if m is None else round(m)}
                        for n, p, m in wins},
        })

    def _key(a):
        pk = _primary_window(a["windows"])
        pct = a["windows"].get(pk, {}).get("left_pct") if pk else None
        return (-SEVERITY.get(a["level"], -1), -(pct if pct is not None else -1))

    return sorted(accounts, key=_key)


def merge_all_accounts(payload, providers, config):
    """all_accounts 대상 provider를 계정별 조회로 채운다. 대표(best) 계정 필드를
    평탄화해 두므로 content·핑·카드 헤더는 단일 계정과 동일하게 동작한다."""
    for prov in providers:
        try:
            accounts = fetch_all_accounts(prov)
            entry = {"ok": True, "multi": accounts}
            if accounts:
                entry.update({k: accounts[0][k]
                              for k in ("email", "level", "action", "reason", "windows")})
            payload.setdefault("providers", {})[prov] = entry
        except Exception as ex:
            payload.setdefault("providers", {})[prov] = {"ok": False, "error": str(ex)[:120]}


def _primary_window(windows):
    for key in ("7d", "daily"):
        if key in windows:
            return key
    return next(iter(windows), None)


# ---------------------------------------------------------------- 디스코드 브리지 세션

# 디스코드에 붙어 있는 봇들 — 봇마다 한 행(폴더 경로 [봇/모델] ctx%)으로 표시.
# 브리지 봇은 config "bridges", 클로드 봇은 "claude_bots"로 교체 가능.
DEFAULT_BRIDGES = [
    {"name": "Codex", "kind": "codex", "dir": "~/ai-folder/dev/codex-discord/data",
     "env": "~/ai-folder/dev/codex-discord/.env"},
    {"name": "Gemini", "kind": "agy", "dir": "~/ai-folder/dev/codex-discord/data-gemini",
     "env": "~/ai-folder/dev/codex-discord/.env.gemini"},
]
CLAUDE_BOTS_DEFAULT = [
    {"name": "Claude", "kind": "claude", "cwd": "~/ai-folder/dev/discord-multiagent"},  # 오케 봇
    {"name": "Claude", "kind": "claude", "cwd": "~/VSCodeWorkspace/discord"},           # 수다 클로드 봇
]
CODEX_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")
CODEX_BASELINE = 12000   # codex TUI가 컨텍스트 % 계산에서 제외하는 기본 오버헤드 토큰


def _pid_alive(path):
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _codex_latest(workdir):
    """workdir에서 가장 최근 활동한 codex 세션(TUI 포함) -> (used%, ts, model).

    exec/TUI 어느 쪽이든 rollout의 session_meta cwd로 판별한다. used%는 codex TUI와
    같은 식(baseline 오버헤드 제외): (total - 12000) / (window - 12000)."""
    files = glob.glob(os.path.join(CODEX_SESSIONS_ROOT, "*", "*", "*", "*.jsonl"))
    files.sort(key=os.path.getmtime, reverse=True)
    for path in files[:400]:
        try:
            with open(path, "rb") as f:
                head = f.readline().decode("utf-8", "replace")
        except OSError:
            continue
        if f'"cwd":"{workdir}"' not in head:
            continue
        mtime = os.path.getmtime(path)
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 131072))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return None, mtime, None
        models = re.findall(r'"model":"([^"]+)"', tail)
        model = models[-1] if models else None
        for line in reversed(tail.splitlines()):
            if '"token_count"' not in line:
                continue
            try:
                info = json.loads(line[line.index("{"):])["payload"]["info"]
                total = info["last_token_usage"]["total_tokens"]
                window = info["model_context_window"]
                used = max(0.0, total - CODEX_BASELINE) / (window - CODEX_BASELINE) * 100
                return used, mtime, model
            except (ValueError, KeyError, TypeError, ZeroDivisionError):
                continue
        return None, mtime, model
    return None, None, None


def _env_var(path, name):
    try:
        for line in open(os.path.expanduser(path)):
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    except (OSError, TypeError):
        pass
    return None


def _fmt_age(ts):
    if not ts:
        return None
    sec = max(0, time.time() - ts)
    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{int(sec // 60)}분 전"
    if sec < 86400:
        return f"{int(sec // 3600)}시간 전"
    return f"{int(sec // 86400)}일 전"


def _shorten_home(path):
    return (path or "").replace(os.path.expanduser("~"), "~")


def collect_bots(config):
    """디스코드 봇별 한 행: 작업 폴더 + ctx%(+ 마지막 활동). statusline 형식과 동일 축."""
    rows = []
    # 브리지 봇(codex/gemini) — 폴더 = CODEX_WORKDIR, ctx·모델 = 최근 활동 세션 rollout
    for b in config.get("bridges", DEFAULT_BRIDGES):
        d = os.path.expanduser(b["dir"])
        workdir_abs = _env_var(b.get("env"), "CODEX_WORKDIR") or ""
        alive = _pid_alive(os.path.join(d, "daemon.pid"))
        used, ts, model = (None, None, None)
        if b["kind"] == "codex" and workdir_abs:
            used, ts, model = _codex_latest(workdir_abs)
        rows.append({"label": _shorten_home(workdir_abs) or "?",
                     "tag": model or b["name"], "kind": b["kind"],
                     "used": used, "ts": ts,
                     "note": None if alive else "브리지 꺼짐"})
    # 클로드 봇(오케 등) — statusline 스냅샷에서 해당 폴더의 최신 세션을 찾는다
    snaps = load_sessions()
    for cb in config.get("claude_bots", CLAUDE_BOTS_DEFAULT):
        cwd = _shorten_home(os.path.expanduser(cb["cwd"]))
        # project_dir(세션을 띄운 폴더) 우선 — 봇이 tasks/ 하위로 cd 해도 행이 끊기지 않는다.
        # project_dir이 없는 옛 스냅샷은 cwd로 폴백.
        best = max((s for s in snaps if (s.get("project_dir") or s.get("cwd")) == cwd),
                   key=lambda s: s.get("ts") or 0, default=None)
        rows.append({"label": cwd,
                     "tag": (best or {}).get("model") or cb.get("name", "Claude"),
                     "kind": "claude",
                     "used": (best or {}).get("used"),
                     "ts": (best or {}).get("ts"), "note": None})
    return rows


# ---------------------------------------------------------------- 세션 스냅샷 (statusline이 기록)

def load_sessions():
    """statusline-command.sh가 남긴 로컬 Claude Code 세션 스냅샷 목록(48시간 지난 파일 청소).
    표시 여부 판단은 호출부(collect_bots — 봇 폴더와 매칭되는 것만)."""
    sessions, now = [], time.time()
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return []
    for name in names:
        path = os.path.join(SESSIONS_DIR, name)
        try:
            if now - os.path.getmtime(path) > 172800:
                os.remove(path)
                continue
            with open(path, encoding="utf-8") as f:
                s = json.load(f)
            home = os.path.expanduser("~")
            s["cwd"] = (s.get("cwd") or "").replace(home, "~")
            if s.get("project_dir"):
                s["project_dir"] = s["project_dir"].replace(home, "~")
            sessions.append(s)
        except (OSError, json.JSONDecodeError):
            continue
    return sessions


def _ctx_emoji(used):
    if used >= 80:
        return "🔴"
    if used >= 40:      # 사용자 마감 습관 기준 — 40%부터 주의
        return "🟡"
    return "🟢"


# ---------------------------------------------------------------- Components V2 조립

BAR_W = 14


def _accent(hex_color):
    return int(hex_color[1:], 16)


def _bar(pct):
    if pct is None:
        return "░" * BAR_W
    filled = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    return "█" * filled + "░" * (BAR_W - filled)


def _reset_rel(reset_min):
    """<t:..:R> — 각 사용자 로컬 기준 '3시간 후'처럼 자동 표시."""
    if not reset_min:
        return None
    return f"<t:{int(time.time() + reset_min * 60)}:R>"


def _accent_for(key, level):
    """평상시 = 회사 브랜드색, 위험/대기(red·yellow·wait) = level 색."""
    if level in ("red", "yellow", "wait"):
        return LEVELS[level][0]
    return PROV_HEX.get(key, LEVELS.get(level, ("#9AA4B2",))[0])


def _win_emoji(pct):
    """신호등 통일 — 평상시 🟢, 잔량 <50% 🟡, <25% 🔴 (미상 ⚪)."""
    if pct is None:
        return "⚪"
    if pct < 25:
        return "🔴"
    if pct < 50:
        return "🟡"
    return "🟢"


def _provider_container(key, p):
    if not p.get("ok"):
        return {"type": 17, "accent_color": _accent("#565B66"), "components": [
            {"type": 10,
             "content": f"### {key.title()}\n⚠️ 조회 실패 — {str(p.get('error', ''))[:120]}"}]}
    level = p.get("level")
    emoji = LEVELS.get(level, ("#9AA4B2", "⚪", ""))[1]
    if level in ("green", "white"):
        emoji = PROV_EMOJI.get(key, emoji)
    head = f"### {emoji} {key.title()} — {p.get('action', '')}"
    if p.get("email"):
        head += f"\n-# {p['email']}"
    lines = []
    if p.get("multi"):
        width = max((len((a.get("email") or "?").split("@", 1)[0]) for a in p["multi"]),
                    default=1)
        for acc in p["multi"]:
            local = (acc.get("email") or "?").split("@", 1)[0]
            pk = _primary_window(acc.get("windows", {}))
            info = acc.get("windows", {}).get(pk, {}) if pk else {}
            pct = info.get("left_pct")
            line = (f"{_win_emoji(pct)} `{local.ljust(width)}  {_bar(pct)}` "
                    f"**{'—' if pct is None else pct}%**")
            reset = _reset_rel(info.get("reset_min"))
            if reset:
                line += f" · 리셋 {reset}"
            lines.append(line)
    else:
        windows = p.get("windows", {})
        lw = max((len(WIN_CODE.get(wk, wk)) for wk in windows), default=0)
        for wk, info in windows.items():
            pct = info.get("left_pct")
            line = (f"{_win_emoji(pct)} `{WIN_CODE.get(wk, wk).ljust(lw)}  {_bar(pct)}` "
                    f"**{'—' if pct is None else pct}%**")
            reset = _reset_rel(info.get("reset_min"))
            if reset:
                line += f" · 리셋 {reset}"
            lines.append(line)
    children = [{"type": 10, "content": head}]
    if lines:
        children.append({"type": 10, "content": "\n".join(lines)})
    if p.get("reason"):
        children.append({"type": 10, "content": f"**{p['reason']}**"})
    return {"type": 17, "accent_color": _accent(_accent_for(key, p.get("level"))),
            "components": children}


def _bots_container(bots):
    rows = []
    heads = [f"{s.get('label') or '?'}  [{s.get('tag') or '?'}]" for s in bots]
    width = max((len(h) for h in heads), default=1)
    for s, head in zip(bots, heads):
        used = s.get("used")
        emoji = "⚪" if (s.get("note") or used is None) else _ctx_emoji(used)
        ctx = f"{round(used)}%" if used is not None else "—"
        line = f"{emoji} `{head.ljust(width)}  {_bar(used)}` **{ctx}**"
        extra = s.get("note") or _fmt_age(s.get("ts"))
        if extra:
            line += f" · {extra}"
        rows.append(line)
    # accent = 가장 빡빡한 세션의 ctx 색 (ctx 미상뿐이면 회색)
    worst = max((s["used"] for s in bots if s.get("used") is not None), default=None)
    color = ("#565B66" if worst is None else
             "#F04747" if worst >= 80 else "#FAA61A" if worst >= 40 else "#43B581")
    return {"type": 17, "accent_color": _accent(color), "components": [
        {"type": 10, "content": "### 봇 세션"},
        {"type": 10, "content": "\n".join(rows)}]}


def build_components(payload, bots):
    provs = payload.get("providers", {})
    comps = [_provider_container(key, provs[key]) for key in provider_order(provs)]
    if bots:
        comps.append(_bots_container(bots))
    comps.append({"type": 10, "content": f"-# 갱신 <t:{int(time.time())}:R>"})
    return comps


# ---------------------------------------------------------------- 디스코드 웹훅

def _request(url, method, payload_json):
    data = None if payload_json is None else json.dumps(payload_json,
                                                        ensure_ascii=False).encode()
    headers = {"User-Agent": "usage-coach-dash"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def upsert_card(webhook, components, state, force_new=False):
    mid = state.get("message_id")
    if mid and not force_new:
        try:
            _request(f"{webhook}/messages/{mid}?with_components=true", "PATCH",
                     {"components": components})
            return mid
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):
                raise
            if e.code == 400:   # V2 이전(PNG 카드) 메시지는 전환 편집 불가 → 지우고 새로
                try:
                    _request(f"{webhook}/messages/{mid}", "DELETE", None)
                except urllib.error.HTTPError:
                    pass
    res = _request(f"{webhook}?wait=true&with_components=true", "POST",
                   {"username": USERNAME, "flags": FLAG_COMPONENTS_V2,
                    "components": components})
    return res.get("id")


def ping_if_worse(webhook, payload, state):
    """provider level이 yellow/red로 악화된 순간에만 새 메시지(푸시)."""
    last = state.get("last_levels", {})
    now = {}
    for key, p in payload.get("providers", {}).items():
        lv = p.get("level") if p.get("ok") else None
        now[key] = lv
        if lv not in ("yellow", "red"):
            continue
        prev = last.get(key)
        if prev is not None and SEVERITY.get(lv, 9) < SEVERITY.get(prev, 9):
            color, emoji, verdict = LEVELS[lv]
            who = key.title()
            if p.get("email"):
                who += f"({p['email'].split('@', 1)[0]})"
            _request(webhook, "POST", {
                "username": USERNAME,
                "content": f"{emoji} **{who}** {verdict} — {p.get('action', '')}",
            })
    return now


# ---------------------------------------------------------------- 설정·상태

def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_webhook(cli_value, config):
    if cli_value:
        return cli_value
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        return os.environ["DISCORD_WEBHOOK_URL"]
    return config.get("webhook_url")


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="usage-coach 디스코드 대시보드 (Components V2)")
    ap.add_argument("--out", metavar="PATH", help="컴포넌트 JSON만 저장(웹훅 미사용)")
    ap.add_argument("--webhook", help="웹훅 URL(설정 파일보다 우선)")
    ap.add_argument("--force-new", action="store_true", help="편집 대신 새 메시지 게시")
    ap.add_argument("--mock", metavar="N", help="coach --mock 전달(1~5|all)")
    ap.add_argument("--file", action="append", default=[], metavar="PATH:PROVIDER",
                    help="coach --file 전달(반복 가능)")
    ap.add_argument("--providers", help="coach --providers 전달")
    ap.add_argument("--account", action="append", default=[], metavar="PROVIDER:LABEL",
                    help="coach --account 전달(반복 가능)")
    args = ap.parse_args()

    config = load_config()
    extra = list(config.get("coach_args", []))   # 예: ["--account", "antigravity:foo@gmail.com"]
    if args.mock:
        extra += ["--mock", args.mock]
    for fspec in args.file:
        extra += ["--file", fspec]
    for aspec in args.account:
        extra += ["--account", aspec]

    # all_accounts 대상 provider(기본 antigravity)는 coach 단일 조회에서 빼고
    # codexbar --all-accounts로 전 계정을 따로 채운다 (mock/file 모드에선 미적용)
    provs_csv = args.providers or "claude,codex,antigravity"
    live = not (args.mock or args.file)
    multi_provs = ([p for p in config.get("all_accounts", ["antigravity"])
                    if p in provs_csv.split(",")] if live else [])
    if live:
        single = [p for p in provs_csv.split(",") if p.strip() and p not in multi_provs]
        extra += ["--providers", ",".join(single)]
    elif args.providers:
        extra += ["--providers", args.providers]

    payload = fetch_payload(extra)
    if multi_provs:
        merge_all_accounts(payload, multi_provs, config)

    components = build_components(payload, collect_bots(config))

    if args.out:
        Path(args.out).write_text(json.dumps(components, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"컴포넌트 JSON 저장: {args.out}")
        return 0

    webhook = load_webhook(args.webhook, config)
    if not webhook:
        print("웹훅 URL이 없어요. ~/.config/usage-coach/discord.json 에 "
              '{"webhook_url": "..."} 를 넣거나 --webhook 로 주세요.', file=sys.stderr)
        return 2

    state = load_state()
    state["last_levels"] = ping_if_worse(webhook, payload, state)
    state["message_id"] = upsert_card(webhook, components, state, args.force_new)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
