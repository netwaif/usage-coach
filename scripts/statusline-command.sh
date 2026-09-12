#!/bin/bash
# A Claude Code statusLine that records session snapshots — the source behind the
#
# dashboard's Claude rows under "bot sessions".
#
# This makes canonical the snapshot contract a local production script had
# (~/.config/usage-coach/sessions/<sid>.json: cwd, project_dir, model, used, ts),
# settled at the third end-to-end gate on 2026-08-05. Before that it lived in the
# author's own private script, so on a fresh account the Claude card stayed
# permanently blank.
# Depends on python3 alone — no jq, to keep what a new account must install small.
# Exits 0 whatever the input: a statusline failure must not disturb the session.
input=$(cat)
STATUSLINE_INPUT="$input" exec python3 - <<'PY'
import json, os, sys, time

try:
    d = json.loads(os.environ.get("STATUSLINE_INPUT", ""))
except ValueError:
    sys.exit(0)

ws = d.get("workspace") or {}
cwd = ws.get("current_dir") or d.get("cwd") or ""
model = (d.get("model") or {}).get("display_name") or ""
used = (d.get("context_window") or {}).get("used_percentage")

home = os.path.expanduser("~")
short = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
line = short + (f" [{model}]" if model else "")
if used is not None:
    line += f" · ctx {round(used)}%"
print(line, end="")

sid = d.get("session_id")
if sid:
    snap_dir = os.path.join(home, ".config/usage-coach/sessions")
    try:
        os.makedirs(snap_dir, exist_ok=True)
        with open(os.path.join(snap_dir, f"{sid}.json"), "w") as f:
            json.dump({"cwd": cwd,
                       "project_dir": ws.get("project_dir") or cwd,
                       "model": model, "used": used,
                       "ts": int(time.time())}, f)
    except OSError:
        pass
PY
