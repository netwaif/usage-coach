#!/usr/bin/env python3
"""Part one, the usage coach — reads codexbar usage and advises a job size, the
reason for it, and the timing when that matters.

Spec: docs/logic-design.md, docs/usage-schema.md
Output: a terminal CLI, colour and a message, with refresh (watch) built in.
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

# ── Tuning, to be adjusted to how you work ───────────────────────────
M = 0.10        # pace margin: the slack when comparing left against timeLeft
T_BIG = 0.40    # the 5h/burst floor below which a big job is not worth starting
T_SOON = 30     # minutes within which a reset counts as "imminent"
WATCH_INTERVAL = 30   # watch mode's default refresh period, in seconds

# ── Part two, the rate guard (--hook) ────────────────────────────────
# A hook runs from an arbitrary cwd, so the config comes from a fixed absolute
# path, and the defaults apply when it is absent.
GUARD_CONFIG_PATH = os.path.expanduser("~/.config/usage-coach/guard.json")
# One vendor-independent runtime switch. Present is on, absent is off;
# `coach guard on|off` toggles it.
GUARD_ENABLED_PATH = os.path.expanduser("~/.config/usage-coach/guard-enabled")
GUARD_DEFAULT = {"m": 0.10, "floor": 0.10, "pace": True}   # per provider: pace margin, absolute floor, pace gate on
GUARD_TARGETS = ["claude", "codex", "antigravity"]   # what the guard covers (gemini == antigravity)
WEEKLY_MINUTES = 10080   # the 7-day window. The guard looks at this one only, ignoring 5h.

# Live query commands, one per provider — `--provider all` hangs
LIVE_CMDS = {
    "claude": ["codexbar", "usage", "--provider", "claude", "--format", "json"],
    "codex":  ["codexbar", "usage", "--provider", "codex", "--source", "oauth", "--format", "json"],  # 2026-08-22: the cli source broke on codex 0.149.0's app-server change → oauth
    "antigravity": ["codexbar", "usage", "--provider", "antigravity", "--format", "json"],
}

# --account PROVIDER:LABEL override, passed through to codexbar --account (filled in from main)
ACCOUNT_OVERRIDES = {}

# ANSI colour, per emoji. Off when this is not a tty, or under NO_COLOR.
_COLORS = {"🔴": "31", "🟢": "32", "⏳": "36", "🟡": "33", "⚪": "37"}
# Bar colour per provider, following the codexbar menu
PROV_COLOR = {"claude": "33", "codex": "36", "antigravity": "35"}
# windowMinutes -> display name
WIN_NAME = {300: "5h", 10080: "7d", 1440: "daily"}
# The extraRateWindows id of the Max-only Fable 5 weekly limit, absent from a standard payload
FABLE_WIN_ID = "claude-weekly-scoped-fable"


def _use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def _paint(emoji, text):
    return _c(f"1;{_COLORS.get(emoji, '37')}", text)


_ANSI = re.compile(r"\033\[[0-9;]*m")


def _w(s):
    """A string's width in terminal cells, ANSI excluded, CJK counted as two."""
    s = _ANSI.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, cells):
    return s + " " * max(0, cells - _w(s))


def _row(left, right, width):
    """`left` flush left and `right` flush right, spaces between them."""
    return left + " " * max(1, width - _w(left) - _w(right)) + right


def _bar(left_frac, code, n=12):
    fill = round(left_frac * n)
    return _c(code, "█" * fill + "░" * (n - fill))


def _dur(mins):
    """Minutes remaining -> `4d 2h` / `3h 53m` / `12m`."""
    mins = round(mins)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def _reset(mins):
    if mins is None:
        return "available"
    return "resets now" if round(mins) < 1 else f"{_dur(mins)} left"


def _term_w():
    return shutil.get_terminal_size((72, 24)).columns


def _wrap(text, width, indent):
    """Wraps `text` to `width` cells on spaces, indenting every line."""
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
    """The windows to display -> [(name, leftPct, mins)]. Invalid ones are skipped.

    Several windows may share a windowMinutes — Claude weekly plus Sonnet weekly
    plus Daily, because codexbar reports per-model and per-feature sub-limits
    against the same seven days. They are **consolidated into the tightest one**:
    whatever has least left is what blocks first, so render, to_payload and
    classify(pick) all look at the same window, and nothing is shown twice or
    disagrees. Exact duplicates collapse for free."""
    best, order = {}, []   # wm -> the tightest r, and the order wm first appeared
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
    return [(WIN_NAME.get(wm, f"{wm}m"), round(best[wm]["left"] * 100), best[wm]["mins"])
            for wm in order]


def fable_window(usage):
    """The Fable 5 weekly limit from extraRateWindows -> (name, leftPct, mins) or None.

    Display only — it is kept out of classify(pick) and the guard, which stay on
    the whole weekly figure. A standard payload has no such entry at all, so this
    answers None and the line is quietly omitted. Matched by id, so another extra
    window such as Daily Routines is not mistaken for it. win() skips an invalid
    window.
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
    """A window dict -> {left, timeLeft, mins, wm}, or None when it is unusable —
    a missing field, an epoch date, something unparseable.

    codexbar sometimes sends windowMinutes, usedPercent or resetsAt as null or
    leaves them out (antigravity, intermittently), so that one window is skipped
    rather than crashing the whole provider.
    """
    if not w:
        return None
    wm, up, rs = w.get("windowMinutes"), w.get("usedPercent"), w.get("resetsAt")
    if not wm or up is None:   # missing, null or 0 → skip
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
    if resets.year < 2000:   # guards against epoch and placeholder dates
        return None
    mins = max(0.0, (resets - now_utc()).total_seconds() / 60.0)
    left = max(0.0, min(1.0, (100 - up) / 100.0))   # guards against out-of-range input
    return {"left": left, "timeLeft": mins / wm, "mins": mins, "wm": wm}


def pick(usage, minutes):
    """Selects a window by windowMinutes — the tightest when several share one.

    The same rule windows_summary uses, so render, json and classify all see the
    same window."""
    best = None
    for k in ("primary", "secondary", "tertiary"):
        r = win(usage.get(k))
        if r and r["wm"] == minutes and (best is None or r["left"] < best["left"]):
            best = r
    return best


def classify(provider, usage):
    """The five outputs of logic-design.md. -> (emoji, action, detail).

    `action` is the recommended job size, the headline; `detail` is why, plus
    the timing when that matters. The coach's three principles hold.

    Decided in order, first match winning:
      #1 🔴 the 7-day (pace) window is tight → the weekly limit is the bottleneck
      #2 🟢 the burst window (5h/daily) is roomy (left>=T_BIG) → room for a big job
      #3 ⏳ burst short, reset imminent (mins<=T_SOON) → wait a moment
      #4 🟡 burst short, behind pace (left<timeLeft-M) → small jobs only
      #5 ⚪ otherwise, keeping pace → carry on as usual
    Conflicts: whichever of 5h and 7d is tighter sets the ceiling, so a tight
    week (#1) outranks a roomy 5h.
    Exception: a pace-only provider with no burst window at all — Antigravity is
    weekly only — is coached 🔴/🟢/⚪ from the 7-day pace alone. The 5h+7d logic
    below is unchanged.
    """
    seven = pick(usage, 10080)             # seven days, the pace window
    short = pick(usage, 300) or pick(usage, 1440)   # the burst window: 5h or daily

    # A pace-only provider with no burst window (Antigravity, weekly only) is coached from the 7-day pace
    if not short:
        if not seven:
            return (None, "no data", "there is no limit window to show.")
        wp = round(seven["left"] * 100)
        if seven["left"] < seven["timeLeft"] - M:
            return ("🔴", "hold the big jobs",
                    f"the weekly limit is tight, {wp}% left, so spending now blocks you "
                    f"later in the week. Run only what you must.")
        if seven["left"] >= T_BIG:
            return ("🟢", "a big job is fine",
                    f"{wp}% of the weekly limit is left, which is ahead of pace. Go bigger than usual.")
        return ("⚪", "carry on as usual",
                f"{wp}% of the weekly limit left and you are keeping pace, so there is room.")

    sp = round(short["left"] * 100)

    # #1 — the 7-day limit is the bottleneck (decided only where a pace window exists, so Gemini skips itself)
    if seven and seven["left"] < seven["timeLeft"] - M:
        return ("🔴", "hold the big jobs",
                f"the 7-day limit is the tightest, {round(seven['left']*100)}% left, so spending "
                f"now blocks you later in the week. Run only the small things you must.")

    # #2 — the burst window is roomy, so there is room for a big job
    if short["left"] >= T_BIG:
        long_ok = " and the long window has room too" if seven else ""
        return ("🟢", "run the big job now",
                f"{sp}% left on the short window{long_ok}, so nothing will cut you off. "
                f"What you leave is lost at the reset, so this is the moment.")

    # #3 — the burst window is short but resets soon, so wait a moment
    if short["mins"] <= T_SOON:
        return ("⏳", "wait a little, then go big",
                f"only {sp}% is left on the short window, but it resets in about "
                f"{_dur(short['mins'])} and you get the lot back.")

    # #4 — the burst window is short and behind pace, so small jobs only
    if short["left"] < short["timeLeft"] - M:
        return ("🟡", "keep the jobs small",
                f"the short window is at {sp}%, so a big job will be cut off part way. "
                f"{_dur(short['mins'])} until it resets.")

    # #5 — otherwise, keeping pace
    return ("⚪", "carry on as usual",
            f"short and long are both on pace, short at {sp}%, so there is room. "
            f"Give only a very large job a second look.")


# ── Input sources ────────────────────────────────────────────────────
def antigravity_gemini_only(usage):
    """Keeps only Gemini out of antigravity's two pools — Gemini is primary,
    Claude and GPT secondary, and those two go unused, so showing them is noise.
    windowMinutes occasionally arrives missing and is corrected to weekly
    (10080): every Antigravity pool is a Weekly Limit."""
    u = dict(usage)
    p = u.get("primary")
    if isinstance(p, dict) and p.get("usedPercent") is not None:
        p = dict(p)
        p.setdefault("windowMinutes", 10080)
    u["primary"], u["secondary"], u["tertiary"] = p, None, None
    return u


def fetch_live(provider):
    """A live codexbar query -> a usage dict. Raises on failure.

    start_new_session=True runs it in a session of its own so it cannot take our
    controlling terminal and foreground group: Ctrl-C (SIGINT) then does not
    reach codexbar, and watch exits cleanly. stdin=DEVNULL keeps it from reading
    our input.
    """
    cmd = list(LIVE_CMDS[provider])
    if provider in ACCOUNT_OVERRIDES:
        cmd += ["--account", ACCOUNT_OVERRIDES[provider]]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=40, start_new_session=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "query failed").strip().splitlines()[-1])
    data = json.loads(out.stdout)
    usage = data[0]["usage"]
    return antigravity_gemini_only(usage) if provider == "antigravity" else usage


def fetch_live_all(provider):
    """codexbar --all-accounts -> one usage dict per registered account."""
    cmd = list(LIVE_CMDS[provider]) + ["--all-accounts"]
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=120, start_new_session=True, stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout or "query failed").strip().splitlines()[-1])
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


# Mock input that raises each of the five outputs on purpose, for filming, when
# the real limits cannot all be reached
def mock_usage(scenario):
    """scenario '1' to '5' -> a usage dict shaped to produce that output."""
    ok7 = _mock_window(0.80, 5040, 10080)   # a roomy week (left .8 > timeLeft .5)
    table = {
        # five(left,mins), seven
        "1": (_mock_window(0.60, 200, 300),  _mock_window(0.20, 6048, 10080)),  # 🔴 the week is the bottleneck
        "2": (_mock_window(0.70, 200, 300),  ok7),                              # 🟢 room for a big job
        "3": (_mock_window(0.20, 10,  300),  ok7),                              # ⏳ reset imminent
        "4": (_mock_window(0.10, 180, 300),  ok7),                              # 🟡 behind pace
        "5": (_mock_window(0.30, 75,  300),  ok7),                              # ⚪ carry on as usual
    }
    five, seven = table[scenario]
    return {"primary": five, "secondary": seven, "tertiary": None}


# ── Rendering ────────────────────────────────────────────────────────
def render(results):
    """results: list[dict] -> the output string: usage per provider and a
    one-line recommendation, aligned, with a rule between blocks."""
    W = max(46, min(_term_w() - 1, 62))
    div = _c("90", " " + "─" * (W - 1))
    out = [_row(_c("1", " usage coach"), _c("90", now_utc().astimezone().strftime("%H:%M")), W), ""]

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
            if pct is None:            # unknown on an account row (no window data)
                left = f"   {_pad(wname, wpad)} {_bar(0, code)}    —"
                b.append(_row(left, _c("90", "unknown"), W))
                continue
            left = f"   {_pad(wname, wpad)} {_bar(pct / 100, code)}  {pct:>3}%"
            b.append(_row(left, _c("90", _reset(mins)), W))
        if r["emoji"]:
            b.append("   " + _paint(r["emoji"], f"{r['emoji']} {r['action']}"))
            b += _wrap(r["detail"], W, "      ")   # why and when, wrapped and aligned under the headline
        elif not r["windows"]:                     # no coaching possible: window data missing or incomplete
            b.append(_c("90", f"   · {r['detail']}"))
        blocks.append(b)

    for i, b in enumerate(blocks):
        out += b
        out.append(div if i < len(blocks) - 1 else "")

    # One overall line, for the tightest case by priority
    order = {"🔴": 0, "🟡": 1, "⏳": 2, "⚪": 3, "🟢": 4}
    valid = [(r["emoji"], r["label"].title()) for r in results if r["ok"] and r["emoji"]]
    if valid:
        we, wl = min(valid, key=lambda x: order.get(x[0], 9))
        verdict = {"🔴": "mind the weekly limit first", "🟡": "hold the big jobs",
                   "⏳": "wait and you get the lot", "⚪": "carry on", "🟢": "a big job is fine"}[we]
        out.append(_paint(we, f" overall  {we} {wl} — {verdict}"))
    return "\n".join(out)


# emoji -> a stable level string, and display name -> JSON key. The agent loop
# branches on both, so neither may follow the display language: the three window
# names happen to read the same in both now, and are still listed rather than
# left to fall through, so renaming one on screen cannot silently rename a key.
_LEVEL = {"🔴": "red", "🟢": "green", "⏳": "wait", "🟡": "yellow", "⚪": "white"}
_WKEY = {"5h": "5h", "7d": "7d", "daily": "daily", "Fable": "fable_7d", "Gemini": "gemini"}


def to_payload(results):
    """Machine output for the agent loop. Carries the coaching — action and
    reason, in full — without loss. Makes no judgement of its own: raw
    per-provider information, with no gate and no overall verdict."""
    provs = {}
    for r in results:
        if not r["ok"]:
            provs[r["label"]] = {"ok": False, "error": r["msg"]}
            continue
        provs[r["label"]] = {
            "ok": True,
            "plan": r.get("plan"),
            "email": r.get("email"),
            "level": _LEVEL.get(r["emoji"]),     # null when no coaching was possible
            "action": r["action"],               # the recommended job size, in full
            "reason": r["detail"],               # why and when, in full and unaltered
            "windows": {_WKEY.get(n, n): {"left_pct": p,
                                          "reset_min": None if m is None else round(m)}
                        for n, p, m in r["windows"]},
        }
    return {"ts": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "providers": provs}


def load_usage(kind, arg):
    """kind (live|live-all|file|mock) + arg -> a usage dict. Raises on failure,
    which the caller handles. For a caller that needs a single usage — the guard,
    say — live-all is narrowed to one query against the selected account."""
    if kind in ("live", "live-all"):
        return fetch_live(arg)
    if kind == "file":
        return load_file(arg)
    return mock_usage(arg)


def gather_all_accounts(label):
    """Every account as one block: a row per account, labelled with the part of
    the email before the @, coached from whichever account has most room."""
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
        return {"label": label, "ok": False, "msg": "no registered account"}
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
    """source(label, kind, arg) -> one result dict."""
    label, kind, arg = source
    try:
        if kind == "live-all":
            return gather_all_accounts(label)
        usage = load_usage(kind, arg)
        emoji, action, detail = classify(label, usage)
        wins = windows_summary(usage)
        if label == "antigravity":   # only Gemini of the two pools, named as it really is
            wins = [("Gemini", pv, mv) for _n, pv, mv in wins]
        fable = fable_window(usage)
        if fable:
            wins.append(fable)     # display only: classify does not see it
        return {
            "label": label, "ok": True,
            "email": usage.get("accountEmail"),
            "plan": usage.get("loginMethod"),
            "windows": wins,
            "emoji": emoji, "action": action, "detail": detail,
        }
    except subprocess.TimeoutExpired:
        return {"label": label, "ok": False, "msg": "query timed out (hung?) — skipped"}
    except Exception as e:
        return {"label": label, "ok": False, "msg": f"query failed: {e}"}


def gather(sources):
    """sources: list[(label, kind, arg)] -> a list of result dicts. kind is one
    of live, live-all, file, mock.

    Queried strictly in sequence: codexbar locks up or hangs when run
    concurrently — measured, two parallel instances hang and the query fails, the
    same family as the `--provider all` hang. At tens of seconds per provider the
    whole run takes 45 seconds to a minute, and that is a codexbar constraint
    parallelism cannot shorten."""
    return [gather_one(s) for s in sources]


# ── Part two, the rate guard (the command hook's decision mode) ──────
def load_guard_config(path=GUARD_CONFIG_PATH):
    """guard.json -> {provider: {m, floor, pace}}. A missing file or key falls
    back to GUARD_DEFAULT.

    Read from a fixed absolute path, because a hook runs from an arbitrary cwd
    and a relative one would not resolve. A parse failure or a missing file
    quietly takes the defaults too: a configuration mistake must not kill
    somebody's work, so this fails open.
    """
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):    # JSON that is not a dict, `[]` say → defaults, failing open
        raw = {}

    def _num(entry, key):
        try:
            return float(entry.get(key, GUARD_DEFAULT[key]))
        except (TypeError, ValueError):   # a non-numeric value, "bad" or null → default
            return GUARD_DEFAULT[key]

    def _pace(entry):
        v = entry.get("pace", GUARD_DEFAULT["pace"])
        return v if isinstance(v, bool) else GUARD_DEFAULT["pace"]   # a broken value → the default, true

    cfg = {}
    for prov in GUARD_TARGETS:
        entry = raw.get(prov)
        if entry is None and prov == "antigravity":   # `gemini` is accepted as an alias
            entry = raw.get("gemini")
        if not isinstance(entry, dict):    # missing, null or a scalar → defaults
            entry = {}
        cfg[prov] = {"m": _num(entry, "m"), "floor": _num(entry, "floor"), "pace": _pace(entry)}
    return cfg


def guard_check(sources, cfg):
    """The hybrid OR guard on the 7-day window. -> a stopReason string, or None
    to let the work through.

    Either condition stops it, per provider:
      left < timeLeft − m   (pace: burning it too fast — skipped when pace:false)
      OR left < floor       (absolute: close to the bottom)
    Fails open: a failed query, or no 7-day windowMinutes, means that provider
    contributes nothing to a stop.
    """
    triggers = []
    for label, kind, arg in sources:
        try:
            usage = load_usage(kind, arg)
            w = pick(usage, WEEKLY_MINUTES)     # the seven days only, never the 5h
        except Exception:
            continue                            # the query failed → fail open
        if not w:
            continue                            # no 7-day window, or an invalid one → fail open
        c = cfg.get(label, GUARD_DEFAULT)
        m, floor, pace = c["m"], c["floor"], c.get("pace", True)
        # A 7-day window resets at most seven days out, so timeLeft ≤ 1.0. Clamped
        # so a corrupted resetsAt far in the future cannot run away and stop work
        # that was fine — the same fail-open principle.
        left, time_left, mins = w["left"], min(1.0, w["timeLeft"]), w["mins"]
        # pace:false means "spend what is left": skip the pace condition, keep the floor.
        if pace and left < time_left - m:
            triggers.append(f"{label} is over its weekly pace — {left*100:.0f}% left "
                            f"< {(time_left - m)*100:.0f}% expected (resets in {_dur(mins)})")
        elif left < floor:
            triggers.append(f"{label} is near the bottom of its weekly limit — "
                            f"{left*100:.0f}% left < the {floor*100:.0f}% floor")
    if not triggers:
        return None
    return "rate guard: " + " · ".join(triggers) + " → stop and take a look."


def read_hook_event():
    """Consumes the event JSON the harness sends on stdin, to avoid a broken
    pipe. Nothing is done with the content.

    Skipped on a tty so a blocking read cannot hang an interactive manual test.
    """
    if sys.stdin.isatty():
        return {}
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except Exception:
        return {}


def guard_enabled():
    """The runtime switch: the flag file being present is on, absent is off."""
    return os.path.exists(GUARD_ENABLED_PATH)


def set_guard(on):
    """Toggles the runtime switch: on creates the flag file, making its parent
    directory as needed, and off deletes it.

    Fails open and stays quiet, so a permissions problem on the config directory
    cannot kill somebody's work.
    """
    try:
        if on:
            os.makedirs(os.path.dirname(GUARD_ENABLED_PATH), exist_ok=True)
            open(GUARD_ENABLED_PATH, "a").close()   # touch an empty flag file
        else:
            try:
                os.remove(GUARD_ENABLED_PATH)
            except FileNotFoundError:
                pass                                # already off
    except Exception:
        pass


def run_guard_cmd(action, sources):
    """`coach guard <on|off|status>` — one vendor-independent runtime switch.

    on and off toggle the flag file; status reports the current state and
    previews what would be decided against usage as it stands.
    """
    if action == "on":
        set_guard(True)
        print("rate guard: on (flag created)")
    elif action == "off":
        set_guard(False)
        print("rate guard: off (flag deleted)")
    else:   # status, the default
        state = "on" if guard_enabled() else "off"
        print(f"rate guard: {state}")
        # A live preview of what would be decided now, shown whatever the switch says.
        try:
            reason = guard_check(sources, load_guard_config())
        except Exception:
            reason = None                           # a query error → no preview, failing open
        if reason:
            print(f"  right now it would stop: {reason}")
        else:
            print("  right now it would pass")


def run_hook(sources):
    """The command Stop hook's decision mode, inside Claude. A stop prints one
    line of {continue:false,...}; a pass prints nothing. Always exits 0.

    Two gates come before the guard proper:
      1) no guard-enabled flag → pass immediately
      2) stop_hook_active in the stdin event is not truthy → pass, so this fires
         only from turn two of a /goal loop onwards
    """
    if not guard_enabled():
        return                                      # switch off → pass, printing nothing
    event = read_hook_event()                       # consume the stdin event, and use it
    if not event.get("stop_hook_active"):
        return                                      # not a loop turn → pass, printing nothing
    reason = guard_check(sources, load_guard_config())
    if reason:
        print(json.dumps({"continue": False, "stopReason": reason}, ensure_ascii=False))
    # A pass prints nothing at all.


def run_guard_check(sources):
    """`coach --guard-check` — the provider-neutral mode for the Codex watcher.
    -> an exit code.

    No loop gate, but the guard-enabled gate still applies: off means pass.
    The output contract: a pass, or no decision, exits 0 and prints nothing; a
    stop exits non-zero (10) and prints one line of reason on stdout.
    """
    if not guard_enabled():
        return 0                                    # switch off → pass, printing nothing
    reason = guard_check(sources, load_guard_config())
    if reason:
        print(reason)
        return 10
    return 0


def _draw(text):
    """Redraws in place without flicker: cursor home, print each line clearing to
    the end of it, then clear whatever is left below."""
    body = "".join(line + "\033[K\n" for line in text.split("\n"))
    sys.stdout.write("\033[H" + body + "\033[J")
    sys.stdout.flush()


def run_watch(sources, interval):
    """Watch mode: refreshes every `interval` seconds, Enter refreshes now,
    q or Ctrl-C exits.

    The query (gather) runs with the previous screen still up, and the redraw
    happens only once new data is ready — so the screen never goes blank while
    refreshing.
    """
    is_tty = sys.stdin.isatty()
    footer = (f"\n  [refreshes every {interval}s · Enter = refresh now · q/Ctrl-C = quit]" if is_tty
              else f"\n  [refreshes every {interval}s · Enter = refresh now · q+Enter/Ctrl-C = quit]")
    sys.stdout.write("\033[2J\033[H  loading usage... "
                     "(codexbar is queried one provider at a time, so this can take a minute)\033[J")
    sys.stdout.flush()
    old_attr = None
    if is_tty:
        # cbreak with echo off, so Enter triggers a refresh instead of printing a
        # newline. In canonical mode the echoed newline only moved the cursor down
        # and nothing said a refresh had started, which read as a dead key. q
        # quits at once, without Enter.
        old_attr = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    try:
        while True:
            frame = render(gather(sources)) + footer   # the previous frame stays up through a slow query
            _draw(frame)
            r, _, _ = select.select([sys.stdin], [], [], interval)
            if r:
                if is_tty:
                    keys = os.read(sys.stdin.fileno(), 1024)
                    if b"q" in keys.lower():
                        break
                elif sys.stdin.readline().strip().lower() == "q":
                    break
            _draw(frame + _c("90", "  refreshing..."))   # says so the moment a query starts
    except KeyboardInterrupt:
        pass
    finally:
        if old_attr is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_attr)
    print("\nbye.")


def _watch_sec(v):
    try:
        iv = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("the interval has to be a whole number of seconds")
    if iv < 1:
        raise argparse.ArgumentTypeError("the interval has to be at least one second")
    return iv


def build_sources(args, p):
    """args(--mock/--file/--providers) -> sources: list[(label, kind, arg)]. kind: mock|file|live.

    Lifted out of main unchanged, behaviour and all. A malformed value exits
    through p.error.
    """
    if args.mock:
        scenarios = ["1", "2", "3", "4", "5"] if args.mock == "all" else [args.mock]
        return [(f"mock#{s}", "mock", s) for s in scenarios]
    if args.file:
        sources = []
        for spec in args.file:
            if ":" not in spec:
                p.error(f"--file takes PATH:PROVIDER (got {spec!r})")
            path, prov = spec.rsplit(":", 1)
            sources.append((prov, "file", path))
        return sources
    return [(prov,
             "live-all" if prov == "antigravity" and prov not in ACCOUNT_OVERRIDES else "live",
             prov)
            for prov in args.providers.split(",") if prov.strip()]


def main():
    p = argparse.ArgumentParser(description="a usage coach over codexbar — recommends a job size")
    p.add_argument("cmd", nargs="?", choices=["guard"],
                   help="subcommand. 'guard' is the rate guard's runtime switch")
    p.add_argument("action", nargs="?", choices=["on", "off", "status"],
                   help="guard action: on|off|status (status when omitted)")
    p.add_argument("--providers", default="claude,codex,antigravity",
                   help="providers to query live, comma separated. All of them by default")
    p.add_argument("--mock", metavar="N",
                   help="mock output: '1' to '5', or 'all' for every one. For filming and checking")
    p.add_argument("--file", action="append", metavar="PATH:PROVIDER", default=[],
                   help="read from a JSON file, e.g. samples/claude.json:claude. Repeatable")
    p.add_argument("--account", action="append", metavar="PROVIDER:LABEL", default=[],
                   help="name a provider's account, passed through to codexbar --account, "
                        "e.g. antigravity:foo@gmail.com. Repeatable")
    p.add_argument("--watch", nargs="?", const=WATCH_INTERVAL, type=_watch_sec, metavar="SEC",
                   help=f"watch mode, refreshing every {WATCH_INTERVAL}s by default. Enter refreshes now")
    p.add_argument("--once", action="store_true",
                   help="print once and exit, no watching. Watch is the default")
    p.add_argument("--json", action="store_true",
                   help="machine output for an agent loop: one line of JSON, once. The coaching in full, no colour or decoration")
    p.add_argument("--hook", action="store_true",
                   help="the command Stop hook's decision mode, on the 7-day guard. A stop prints {continue:false}, a pass prints nothing")
    p.add_argument("--guard-check", action="store_true", dest="guard_check",
                   help="the guard check for the Codex watcher, provider-neutral. A stop exits non-zero with a reason, a pass exits 0 silently")
    args = p.parse_args()

    for spec in args.account:
        if ":" not in spec:
            p.error(f"--account takes PROVIDER:LABEL (got {spec!r})")
        prov, label = spec.split(":", 1)
        ACCOUNT_OVERRIDES[prov] = label

    sources = build_sources(args, p)

    # The guard subcommand: the runtime switch, on/off/status, once.
    if args.cmd == "guard":
        run_guard_cmd(args.action, sources)
    # --hook: the command Stop hook's decision mode, once. A stop prints one line of JSON, a pass nothing.
    elif args.hook:
        run_hook(sources)
    # --guard-check: once, for the watcher. A stop exits non-zero with a reason, a pass exits 0 silently.
    elif args.guard_check:
        sys.exit(run_guard_check(sources))
    # --json: machine output for an agent loop, once. Otherwise watch by default, or one human-readable print with --once.
    elif args.json:
        print(json.dumps(to_payload(gather(sources)), ensure_ascii=False, separators=(",", ":")))
    elif args.once:
        print(render(gather(sources)))
    else:
        run_watch(sources, args.watch if args.watch is not None else WATCH_INTERVAL)


if __name__ == "__main__":
    main()
