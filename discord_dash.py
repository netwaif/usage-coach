#!/usr/bin/env python3
"""The usage-coach Discord dashboard — assembles `coach --json` into a
Components V2 message, a container per provider with a Unicode gauge bar, and
keeps editing (upserting) one webhook message. A level getting worse pings as a
new message.

Run by a LaunchAgent on a schedule, every five minutes by default. For a manual
check, --out writes the JSON and nothing else.
Config: ~/.config/usage-coach/discord.json {"webhook_url": "..."}, or the
DISCORD_WEBHOOK_URL environment variable.
State: ~/.config/usage-coach/discord-state.json {"message_id", "last_levels"}

All of it is native text, so it stays sharp at any width on desktop and mobile.
The PNG card this replaced was scaled down as a whole the taller it got, which
made it unreadable on a desktop. Sending components through a webhook needs
?with_components=true in the URL, or the fields are ignored (50006).
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

import coach as coachmod   # reuses the decision policy (classify and the rest) — coach is canonical

COACH = Path(__file__).with_name("coach.py")
CONFIG_PATH = os.path.expanduser("~/.config/usage-coach/discord.json")
STATE_PATH = os.path.expanduser("~/.config/usage-coach/discord-state.json")
SESSIONS_DIR = os.path.expanduser("~/.config/usage-coach/sessions")
USERNAME = "dashboard"
FLAG_COMPONENTS_V2 = 32768   # 1 << 15

# level -> (colour, emoji, the overall line) — the same axis as coach.py's order and verdict
LEVELS = {
    "red":    ("#F04747", "🔴", "mind the weekly limit first"),
    "yellow": ("#FAA61A", "🟡", "hold the big jobs"),
    "wait":   ("#7C8AFF", "⏳", "wait and you get the lot"),
    "white":  ("#9AA4B2", "⚪", "carry on"),
    "green":  ("#43B581", "🟢", "a big job is fine"),
}
SEVERITY = {"red": 0, "yellow": 1, "wait": 2, "white": 3, "green": 4}   # lower is worse
# Gauge labels — ASCII only, because they have to line up with the bar inside a
# code span and a CJK character's monospace width differs between clients, which
# shears the row
WIN_CODE = {"5h": "5h", "7d": "7d", "daily": "1d", "fable_7d": "Fable", "gemini": "Gemini"}
# Provider brand colours, the container accent in the quiet case. Anything at
# risk — red, yellow, wait — is overridden by the level colour
PROV_HEX = {"claude": "#E5B567", "codex": "#7ED5F5", "antigravity": "#C89BF0"}
# The header dot follows the same rule, approximating the brand within what the
# emoji palette allows
PROV_EMOJI = {"claude": "🟠", "codex": "🔵", "antigravity": "🟣"}


# ------------------------------------------------------------- calling coach

def fetch_payload(extra_args):
    cmd = [sys.executable, str(COACH), "--json", "--once"] + extra_args
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"coach --json failed (exit {out.returncode}): {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def provider_order(provs):
    order = [p for p in ("claude", "codex", "antigravity") if p in provs]
    return order + [p for p in provs if p not in order]


def fetch_all_accounts(provider):
    """Decides every account by coach's own policy, one at a time — for
    antigravity, coach keeps only the Gemini pool. Returned sorted by how much
    room each has: best level first, then most left."""
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
    """Fills in an all_accounts provider from the per-account queries. The best
    account's fields are flattened up, so the content, the ping and the card
    header behave exactly as they do for a single account."""
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


# --------------------------------------------------- Discord bridge sessions

# The bots attached to Discord — one row each: the folder path, [bot/model], ctx%.
# Bridge bots can be replaced through the config's "bridges", Claude bots through
# "claude_bots".
DEFAULT_BRIDGES = [
    {"name": "Codex", "kind": "codex", "dir": "~/ai-folder/dev/codex-discord/data",
     "env": "~/ai-folder/dev/codex-discord/.env"},
    {"name": "Gemini", "kind": "agy", "dir": "~/ai-folder/dev/codex-discord/data-gemini",
     "env": "~/ai-folder/dev/codex-discord/.env.gemini"},
]
CLAUDE_BOTS_DEFAULT = [
    {"name": "Claude", "kind": "claude", "cwd": "~/ai-folder/dev/discord-multiagent"},  # the orchestrator bot
    {"name": "Claude", "kind": "claude", "cwd": "~/VSCodeWorkspace/discord"},           # the chat Claude bot
]
CODEX_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")
CODEX_BASELINE = 12000   # the overhead tokens the codex TUI leaves out of its context %
FOLDER_BOTS_PATH = os.path.expanduser("~/.config/folder-bot/bots.json")


def _folder_bots():
    """The folder-bot plugin's canonical bot list (bots.json). An empty dict when
    it is not installed, or is broken."""
    try:
        with open(FOLDER_BOTS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _pid_alive(path):
    try:
        pid = int(open(path).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _codex_latest(workdir):
    """The most recently active codex session in `workdir`, the TUI included ->
    (used%, ts, model).

    exec and TUI alike are identified by the rollout's session_meta cwd. used% is
    computed the way the codex TUI does it, leaving the baseline overhead out:
    (total - 12000) / (window - 12000)."""
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
        if model is None:
            # The model key sits in the opening turns (session_meta, turn_context),
            # so in a large rollout it falls outside the tail window — measured on
            # collab: a 361KB file with the key between 100KB and 146KB. When the
            # tail does not have it, the whole file is read once, which costs
            # nothing at a five-minute period.
            try:
                with open(path, "rb") as f:
                    whole = f.read().decode("utf-8", "replace")
                models = re.findall(r'"model":"([^"]+)"', whole)
                model = models[-1] if models else None
            except OSError:
                pass
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
        return "just now"
    if sec < 3600:
        return f"{int(sec // 60)}m ago"
    if sec < 86400:
        return f"{int(sec // 3600)}h ago"
    return f"{int(sec // 86400)}d ago"


def _shorten_home(path):
    return (path or "").replace(os.path.expanduser("~"), "~")


def collect_bots(config):
    """One row per Discord bot: the working folder and ctx%, plus when it was last
    active. The same axis the statusline uses."""
    rows = []
    # Bridge bots (codex/gemini) — folder from CODEX_WORKDIR, ctx and model from
    # the most recently active session's rollout
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
                     "note": None if alive else "bridge down"})
    # Claude bots, the orchestrator among them — the newest session for that
    # folder, out of the statusline snapshots
    snaps = load_sessions()
    for cb in config.get("claude_bots", CLAUDE_BOTS_DEFAULT):
        cwd = _shorten_home(os.path.expanduser(cb["cwd"]))
        # project_dir — the folder the session was started in — takes precedence,
        # so a bot that cds into tasks/ does not lose its row. An older snapshot
        # without project_dir falls back to cwd.
        best = max((s for s in snaps if (s.get("project_dir") or s.get("cwd")) == cwd),
                   key=lambda s: s.get("ts") or 0, default=None)
        rows.append({"label": cwd,
                     "tag": (best or {}).get("model") or cb.get("name", "Claude"),
                     "kind": "claude",
                     "used": (best or {}).get("used"),
                     "ts": (best or {}).get("ts"), "note": None})
    # folder-bot discovery — a folder bot registered in
    # ~/.config/folder-bot/bots.json gets a row without this config being touched.
    # The folder-bot plugin is canonical for that list.
    seen = {r["label"] for r in rows}
    for name, fb in _folder_bots().items():
        folder = _shorten_home(os.path.expanduser(fb.get("folder", "")))
        if not folder or folder in seen:
            continue
        engine = fb.get("engine", "claude")
        if engine == "claude":
            best = max((s for s in snaps if (s.get("project_dir") or s.get("cwd")) == folder),
                       key=lambda s: s.get("ts") or 0, default=None)
            rows.append({"label": folder,
                         "tag": (best or {}).get("model") or "Claude",
                         "kind": "claude",
                         "used": (best or {}).get("used"),
                         "ts": (best or {}).get("ts"), "note": None})
        elif engine == "codex":
            bridge = os.path.expanduser(fb.get("bridge_dir", "~/codex-discord"))
            alive = _pid_alive(os.path.join(bridge, f"data-{name}", "daemon.pid"))
            used, ts, model = _codex_latest(os.path.expanduser(fb["folder"]))
            rows.append({"label": folder, "tag": model or "Codex", "kind": "codex",
                         "used": used, "ts": ts,
                         "note": None if alive else "bridge down"})
    return rows


# ------------------------------- session snapshots, written by the statusline

def load_sessions():
    """The local Claude Code session snapshots statusline-command.sh leaves
    behind, cleaning up anything older than 48 hours. What to show is the
    caller's decision — collect_bots keeps only what matches a bot folder."""
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
    if used >= 40:      # from 40%, going by when people actually start wrapping up
        return "🟡"
    return "🟢"


# ------------------------------------------------- assembling Components V2

BAR_W = 14


def _accent(hex_color):
    return int(hex_color[1:], 16)


def _bar(pct):
    if pct is None:
        return "░" * BAR_W
    filled = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    return "█" * filled + "░" * (BAR_W - filled)


def _reset_rel(reset_min):
    """<t:..:R> — Discord renders this as "in 3 hours" in each reader's own
    locale and timezone."""
    if not reset_min:
        return None
    return f"<t:{int(time.time() + reset_min * 60)}:R>"


def _accent_for(key, level):
    """The brand colour in the quiet case; the level colour for anything at risk
    or waiting (red, yellow, wait)."""
    if level in ("red", "yellow", "wait"):
        return LEVELS[level][0]
    return PROV_HEX.get(key, LEVELS.get(level, ("#9AA4B2",))[0])


def _win_emoji(pct):
    """One traffic light everywhere: 🟢 normally, 🟡 under 50% left, 🔴 under 25%,
    ⚪ when it is unknown."""
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
             "content": f"### {key.title()}\n⚠️ query failed — {str(p.get('error', ''))[:120]}"}]}
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
                line += f" · resets {reset}"
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
                line += f" · resets {reset}"
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
    # The accent is the ctx colour of the tightest session; grey when every ctx is unknown
    worst = max((s["used"] for s in bots if s.get("used") is not None), default=None)
    color = ("#565B66" if worst is None else
             "#F04747" if worst >= 80 else "#FAA61A" if worst >= 40 else "#43B581")
    return {"type": 17, "accent_color": _accent(color), "components": [
        {"type": 10, "content": "### bot sessions"},
        {"type": 10, "content": "\n".join(rows)}]}


def build_components(payload, bots):
    provs = payload.get("providers", {})
    comps = [_provider_container(key, provs[key]) for key in provider_order(provs)]
    if bots:
        comps.append(_bots_container(bots))
    comps.append({"type": 10, "content": f"-# updated <t:{int(time.time())}:R>"})
    return comps


# ---------------------------------------------------------- the Discord webhook

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
            if e.code == 400:   # a pre-V2 message (the PNG card) cannot be edited into one → delete and repost
                try:
                    _request(f"{webhook}/messages/{mid}", "DELETE", None)
                except urllib.error.HTTPError:
                    pass
    res = _request(f"{webhook}?wait=true&with_components=true", "POST",
                   {"username": USERNAME, "flags": FLAG_COMPONENTS_V2,
                    "components": components})
    return res.get("id")


def ping_if_worse(webhook, payload, state):
    """A new message — a push — only at the moment a provider's level worsens to
    yellow or red."""
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


# ------------------------------------------------------- config and state

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
    ap = argparse.ArgumentParser(description="the usage-coach Discord dashboard (Components V2)")
    ap.add_argument("--out", metavar="PATH", help="write the component JSON and nothing else, using no webhook")
    ap.add_argument("--webhook", help="webhook URL, taking precedence over the config file")
    ap.add_argument("--force-new", action="store_true", help="post a new message instead of editing")
    ap.add_argument("--mock", metavar="N", help="passed to coach --mock (1-5|all)")
    ap.add_argument("--file", action="append", default=[], metavar="PATH:PROVIDER",
                    help="passed to coach --file (repeatable)")
    ap.add_argument("--providers", help="passed to coach --providers")
    ap.add_argument("--account", action="append", default=[], metavar="PROVIDER:LABEL",
                    help="passed to coach --account (repeatable)")
    args = ap.parse_args()

    config = load_config()
    extra = list(config.get("coach_args", []))   # e.g. ["--account", "antigravity:foo@gmail.com"]
    if args.mock:
        extra += ["--mock", args.mock]
    for fspec in args.file:
        extra += ["--file", fspec]
    for aspec in args.account:
        extra += ["--account", aspec]

    # An all_accounts provider — antigravity by default — is left out of coach's
    # single query and filled in separately from codexbar --all-accounts. Not
    # applied in mock or file mode.
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
        print(f"component JSON written: {args.out}")
        return 0

    webhook = load_webhook(args.webhook, config)
    if not webhook:
        print("no webhook URL. Put "
              '{"webhook_url": "..."} in ~/.config/usage-coach/discord.json, '
              "or pass --webhook.", file=sys.stderr)
        return 2

    state = load_state()
    state["last_levels"] = ping_if_worse(webhook, payload, state)
    state["message_id"] = upsert_card(webhook, components, state, args.force_new)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
