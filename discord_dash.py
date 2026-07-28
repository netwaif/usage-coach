#!/usr/bin/env python3
"""usage-coach Discord 대시보드 — coach --json 출력을 메시지 본문(핵심 한 줄씩) +
PNG 게이지 카드로 렌더해 디스코드 웹훅 메시지 하나를 계속 편집(업서트)한다.
level이 나빠지면 새 메시지로 핑.

실행: LaunchAgent가 주기 실행(기본 5분). 수동 검증은 --out 으로 렌더만.
설정: ~/.config/usage-coach/discord.json {"webhook_url": "..."} 또는 env DISCORD_WEBHOOK_URL
상태: ~/.config/usage-coach/discord-state.json {"message_id", "last_levels"}

본문 텍스트가 '즉시 읽는 층'(디스코드 기본 폰트 — 프리뷰 축소 무관),
카드는 '비주얼 상세 층'. 디스코드는 세로가 긴 이미지를 높이 기준으로 줄이므로
카드는 헤더 없이 밀도를 높여 축소 손실을 최소화한다.
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import coach as coachmod   # 판정 정책 재사용(classify 등) — 정본은 coach

COACH = Path(__file__).with_name("coach.py")
CONFIG_PATH = os.path.expanduser("~/.config/usage-coach/discord.json")
STATE_PATH = os.path.expanduser("~/.config/usage-coach/discord-state.json")
SESSIONS_DIR = os.path.expanduser("~/.config/usage-coach/sessions")
FILENAME = "usage-card.png"

# level -> (색, 이모지, 종합 한 줄) — coach.py의 order/verdict와 동일 축
LEVELS = {
    "red":    ("#F04747", "🔴", "주간 한도부터 챙기세요"),
    "yellow": ("#FAA61A", "🟡", "큰 작업은 미루세요"),
    "wait":   ("#7C8AFF", "⏳", "잠깐 기다리면 풀로 가능"),
    "white":  ("#9AA4B2", "⚪", "평소대로"),
    "green":  ("#43B581", "🟢", "큰 작업 OK"),
}
SEVERITY = {"red": 0, "yellow": 1, "wait": 2, "white": 3, "green": 4}   # 낮을수록 나쁨
WIN_KO = {"5h": "5시간", "7d": "7일", "daily": "일일", "fable_7d": "Fable", "gemini": "Gemini"}
# provider별 색 — coach.py PROV_COLOR(터미널 33/36/35)의 카드 버전
PROV_HEX = {"claude": "#E5B567", "codex": "#7ED5F5", "antigravity": "#C89BF0"}

GOTHIC = "/System/Library/Fonts/AppleSDGothicNeo.ttc"


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
        best = max((s for s in snaps if s.get("cwd") == cwd),
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
            s["cwd"] = (s.get("cwd") or "").replace(os.path.expanduser("~"), "~")
            sessions.append(s)
        except (OSError, json.JSONDecodeError):
            continue
    return sessions


def _ctx_color(used):
    if used is None:
        return "#565B66"
    if used >= 80:
        return "#F04747"
    if used >= 40:      # 사용자 마감 습관 기준 — 40%부터 주의
        return "#FAA61A"
    return "#43B581"


# ---------------------------------------------------------------- 본문 텍스트 (즉시 읽는 층)

def build_content(payload):
    lines = []
    provs = payload.get("providers", {})
    for key in provider_order(provs):
        p = provs[key]
        if not p.get("ok"):
            lines.append(f"⚠️ **{key.title()}** 조회 실패")
            continue
        emoji = LEVELS.get(p.get("level"), (None, "⚪", None))[1]
        pk = _primary_window(p.get("windows", {}))
        pct = p.get("windows", {}).get(pk, {}).get("left_pct") if pk else None
        head = f"{emoji} **{key.title()}**"
        if p.get("multi") and p.get("email"):
            head += f" {p['email'].split('@', 1)[0]}"   # 전 계정 중 추천(최여유) 계정
        if pct is not None:
            head += f" {WIN_KO.get(pk, pk)} {pct}%"
        lines.append(f"{head} · {p.get('action', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 렌더러

_FONT_INDEX = {}   # bold 여부 -> ttc index 캐시


def _font(size, bold=False):
    if not _FONT_INDEX:
        want = {"regular": 0, "bold": 0}
        for i in range(12):
            try:
                name = ImageFont.truetype(GOTHIC, 12, index=i).getname()[1].lower()
            except OSError:
                break
            if name == "bold":
                want["bold"] = i
            if name == "regular":
                want["regular"] = i
        _FONT_INDEX.update(want)
    idx = _FONT_INDEX["bold" if bold else "regular"]
    return ImageFont.truetype(GOTHIC, size, index=idx)


def _fmt_reset(mins):
    if not mins:
        return None
    h, m = divmod(int(mins), 60)
    if h >= 48:
        d, h = divmod(h, 24)
        return f"{d}d{h}h"
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) > max_w and cur:
            lines.append(cur)
            cur = ch.lstrip()
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def _hexrgb(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _blend(fg, bg, t):
    f, b = _hexrgb(fg), _hexrgb(bg)
    return "#%02X%02X%02X" % tuple(int(fv * t + bv * (1 - t)) for fv, bv in zip(f, b))


def _fit_left(draw, text, font, max_w):
    """넘치면 앞쪽을 잘라 '…' + 꼬리(디렉토리는 끝부분이 정보량이 큼)."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength("…" + text, font=font) > max_w:
        text = text[1:]
    return "…" + text


def render(payload, sessions=None, w=520, scale=2):
    provs = payload.get("providers", {})
    order = provider_order(provs)
    sessions = sessions or []

    levels = [p["level"] for p in provs.values() if p.get("ok") and p.get("level")]
    worst = min(levels, key=lambda l: SEVERITY.get(l, 9), default=None)
    worst_color = LEVELS.get(worst, ("#565B66",))[0]

    def S(v):
        return int(v * scale)

    # 섹션 높이 선계산 (reason 줄바꿈 측정용 더미 draw)
    dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    f_reason = _font(S(14))
    sections = []
    for key in order:
        p = provs[key]
        ok = p.get("ok")
        windows = p.get("windows", {}) if ok else {}
        reason = p.get("reason", "") if ok else str(p.get("error", ""))[:120]
        lines = _wrap(dummy, reason, f_reason, S(w - 28 - 16))
        if len(lines) > 2:
            lines = [lines[0], lines[1][:-1] + "…"]
        rows = len(p["multi"]) if p.get("multi") else len(windows)
        h_sec = 30 + rows * 25 + 4 + len(lines) * 20 + 10
        sections.append((key, p, windows, lines, h_sec))

    sess_h = (40 + len(sessions) * 25) if sessions else 0
    h = 16 + sum(s[4] for s in sections) + 12 * (len(sections) - 1) + sess_h + 24
    W, H = w * scale, h * scale

    # 배경 세로 그라데이션
    grad = Image.new("RGB", (1, H))
    top, bot = (17, 19, 26), (9, 10, 14)
    for y in range(H):
        t = y / H
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(top, bot)))
    img = grad.resize((W, H))
    d = ImageDraw.Draw(img)
    BG = "#12141A"

    d.rectangle([0, 0, W, S(3)], fill=worst_color)
    y = 16
    for si, (key, p, windows, lines, h_sec) in enumerate(sections):
        ok = p.get("ok")
        pcolor = PROV_HEX.get(key, "#9AA4B2")
        lcolor = LEVELS.get(p.get("level"), ("#9AA4B2",))[0]

        # 이름 + 코칭 action + 계정 이메일 한 줄
        f_name = _font(S(20), True)
        d.text((S(16), S(y + 2)), key.title(), font=f_name, fill=pcolor)
        name_w = d.textlength(key.title(), font=f_name) / scale
        dot_x = max(160, 16 + name_w + 16)
        d.ellipse([S(dot_x), S(y + 10), S(dot_x + 12), S(y + 22)], fill=lcolor)
        action = p.get("action", "") if ok else "조회 실패"
        f_action = _font(S(17), True)
        d.text((S(dot_x + 20), S(y + 4)), action, font=f_action, fill=lcolor)
        email = p.get("email") or ""
        if email:
            action_end = dot_x + 20 + d.textlength(action, font=f_action) / scale
            f_email = _font(S(12))
            if d.textlength(email, font=f_email) / scale > w - 16 - action_end - 16:
                email = email.split("@", 1)[0]   # 공간 부족 시 @ 앞부분만
            ew = d.textlength(email, font=f_email)
            d.text((W - S(16) - ew, S(y + 9)), email, font=f_email, fill="#6B7280")
        y += 30

        track = _blend(pcolor, BG, 0.22)
        multi = p.get("multi")
        if multi:
            # 계정별 바 — 라벨 = 이메일 @ 앞부분, 바 = Gemini(주 윈도우) 잔량
            for acc in multi:
                alc = LEVELS.get(acc.get("level"), ("#565B66",))[0]
                d.ellipse([S(28), S(y + 7), S(28 + 9), S(y + 16)], fill=alc)
                local = (acc.get("email") or "?").split("@", 1)[0]
                d.text((S(42), S(y + 4)), local, font=_font(S(13)), fill="#8B93A2")
                pk = _primary_window(acc.get("windows", {}))
                info = acc.get("windows", {}).get(pk, {}) if pk else {}
                d.rounded_rectangle([S(132), S(y + 6), S(132 + 206), S(y + 18)],
                                    radius=S(6), fill=track)
                pct = info.get("left_pct")
                if pct:
                    d.rounded_rectangle([S(132), S(y + 6),
                                         S(132 + 206 * pct / 100), S(y + 18)],
                                        radius=S(6), fill=pcolor)
                d.text((S(132 + 206 + 8), S(y + 3)),
                       f"{pct}%" if pct is not None else "—",
                       font=_font(S(15), True), fill="#E7EAF0")
                reset = _fmt_reset(info.get("reset_min"))
                if reset:
                    rw = d.textlength(f"{reset} 남음", font=_font(S(13)))
                    d.text((W - S(16) - rw, S(y + 4)), f"{reset} 남음",
                           font=_font(S(13)), fill="#6B7280")
                y += 25
            windows = {}   # 아래 단일 윈도우 루프 생략

        # 윈도우 바 (provider 색)
        for wk, info in windows.items():
            d.text((S(28), S(y + 4)), WIN_KO.get(wk, wk), font=_font(S(14)), fill="#8B93A2")
            bar_x, bar_w = 88, 250
            d.rounded_rectangle([S(bar_x), S(y + 6), S(bar_x + bar_w), S(y + 18)],
                                radius=S(6), fill=track)
            pct = info.get("left_pct") or 0
            if pct > 0:
                d.rounded_rectangle([S(bar_x), S(y + 6), S(bar_x + bar_w * pct / 100), S(y + 18)],
                                    radius=S(6), fill=pcolor)
            d.text((S(bar_x + bar_w + 8), S(y + 3)), f"{pct}%", font=_font(S(15), True),
                   fill="#E7EAF0")
            reset = _fmt_reset(info.get("reset_min"))
            if reset:
                rw = d.textlength(f"{reset} 남음", font=_font(S(13)))
                d.text((W - S(16) - rw, S(y + 4)), f"{reset} 남음", font=_font(S(13)),
                       fill="#6B7280")
            y += 25

        # reason
        y += 4
        for ln in lines:
            d.text((S(28), S(y)), ln, font=f_reason, fill="#8B93A2")
            y += 20
        y += 10

        if si < len(sections) - 1:
            d.line([S(16), S(y + 2), W - S(16), S(y + 2)], fill="#232833", width=S(1))
            y += 12

    # 봇 세션 섹션 — 디스코드에 붙은 봇별 폴더 경로 [봇/모델] ctx%
    if sessions:
        d.line([S(16), S(y + 2), W - S(16), S(y + 2)], fill="#232833", width=S(1))
        y += 14
        d.text((S(16), S(y)), "봇 세션", font=_font(S(16), True), fill="#C9D1DE")
        cnt = f"{len(sessions)}개"
        cw = d.textlength(cnt, font=_font(S(12)))
        d.text((W - S(16) - cw, S(y + 4)), cnt, font=_font(S(12)), fill="#6B7280")
        y += 26
        f_row = _font(S(13))
        f_sub = _font(S(12))
        for s in sessions:
            note = s.get("note")
            used = s.get("used")
            color = "#565B66" if note else _ctx_color(used)
            d.ellipse([S(28), S(y + 5), S(28 + 9), S(y + 14)], fill=color)
            ctx_s = f"{round(used)}%" if used is not None else "—"
            ctx_w = d.textlength(ctx_s, font=_font(S(14), True))
            d.text((W - S(16) - ctx_w, S(y + 1)), ctx_s, font=_font(S(14), True), fill=color)
            tag = f"[{s.get('tag') or '?'}]"
            tag_color = PROV_HEX.get({"codex": "codex", "agy": "antigravity",
                                      "claude": "claude"}.get(s.get("kind"), ""), "#8B93A2")
            tw_ = d.textlength(tag, font=f_row)
            mx = w - 16 - ctx_w / scale - 8 - tw_ / scale
            d.text((S(mx), S(y + 2)), tag, font=f_row, fill=tag_color)
            extra = note or _fmt_age(s.get("ts"))
            extra_w = (d.textlength(f" · {extra}", font=f_sub) / scale + 4) if extra else 0
            label = _fit_left(d, s.get("label") or "?", f_row, S(mx - 42 - 8 - extra_w))
            d.text((S(42), S(y + 2)), label, font=f_row, fill="#C9D1DE")
            if extra:
                lx = 42 + d.textlength(label, font=f_row) / scale
                d.text((S(lx), S(y + 3)), f" · {extra}", font=f_sub, fill="#6B7280")
            y += 25

    ts_local = datetime.now().strftime("%m/%d %H:%M")
    tw = d.textlength(f"{ts_local} 갱신", font=_font(S(12)))
    d.text((W - S(16) - tw, H - S(22)), f"{ts_local} 갱신", font=_font(S(12)), fill="#565B66")

    out = img.resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- 디스코드 웹훅

def _multipart(payload_json, png):
    boundary = "----coachdash" + uuid.uuid4().hex
    body = io.BytesIO()

    def part(s):
        body.write(s.encode() if isinstance(s, str) else s)

    part(f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
         f"Content-Type: application/json\r\n\r\n{json.dumps(payload_json, ensure_ascii=False)}\r\n")
    part(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; "
         f"filename=\"{FILENAME}\"\r\nContent-Type: image/png\r\n\r\n")
    part(png)
    part(f"\r\n--{boundary}--\r\n")
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


def _request(url, method, payload_json, png=None):
    if png is not None:
        data, ctype = _multipart(payload_json, png)
    else:
        data, ctype = json.dumps(payload_json, ensure_ascii=False).encode(), "application/json"
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": ctype, "User-Agent": "usage-coach-dash"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def upsert_card(webhook, png, content, state, force_new=False):
    body = {"content": content, "attachments": [{"id": 0, "filename": FILENAME}]}
    mid = state.get("message_id")
    if mid and not force_new:
        try:
            _request(f"{webhook}/messages/{mid}", "PATCH", body, png)
            return mid
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    res = _request(f"{webhook}?wait=true", "POST",
                   {"username": "Usage Coach", **body}, png)
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
                "username": "Usage Coach",
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
    ap = argparse.ArgumentParser(description="usage-coach 디스코드 게이지 카드")
    ap.add_argument("--out", metavar="PATH", help="PNG만 렌더해 저장(웹훅 미사용)")
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

    png = render(payload, collect_bots(config))

    if args.out:
        Path(args.out).write_bytes(png)
        print(f"렌더 완료: {args.out} ({len(png) // 1024}KB)")
        print(build_content(payload))
        return 0

    webhook = load_webhook(args.webhook, config)
    if not webhook:
        print("웹훅 URL이 없어요. ~/.config/usage-coach/discord.json 에 "
              '{"webhook_url": "..."} 를 넣거나 --webhook 로 주세요.', file=sys.stderr)
        return 2

    state = load_state()
    state["last_levels"] = ping_if_worse(webhook, payload, state)
    state["message_id"] = upsert_card(webhook, png, build_content(payload), state,
                                      args.force_new)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
