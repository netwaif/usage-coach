#!/usr/bin/env bash
# Installs the usage-coach dashboard's LaunchAgent — discord_dash.py every five minutes.
# DRY_RUN=1 skips launchctl and only writes the plist. That is the test contract.
set -euo pipefail
fail() { echo "error: $*" >&2; exit 1; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Per OS: macOS uses a launchd LaunchAgent, Linux a systemd user unit (service plus timer). HARNESS_OS overrides it for tests.
OS_NAME="${HARNESS_OS:-$(uname -s)}"
case "$OS_NAME" in Darwin|Linux) ;; *) fail "unsupported OS: $OS_NAME (macOS and Linux only)" ;; esac
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PYTHON_BIN" ]] || fail "python3 not found"
[[ -f "$HOME/.config/usage-coach/discord.json" || -n "${DISCORD_WEBHOOK_URL:-}" ]] \
  || fail "webhook_url is not set — create ~/.config/usage-coach/discord.json with {\"webhook_url\": \"...\"} first"

if [[ "$OS_NAME" == "Linux" ]]; then
  # A systemd user unit — the timer wakes the service every five minutes. On a VPS,
  # loginctl enable-linger runs it from boot without anybody logging in. On WSL2 it
  # runs only while Ubuntu is up, so leave a terminal open.
  SVC="usage-coach-dashboard"
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR" "$DIR/logs"
  cat > "$UNIT_DIR/$SVC.service" <<EOF
[Unit]
Description=usage-coach Discord dashboard (every five minutes, woken by the timer)

[Service]
Type=oneshot
WorkingDirectory=$DIR
Environment="PATH=$PATH"
ExecStart=$PYTHON_BIN $DIR/discord_dash.py
StandardOutput=append:$DIR/logs/dashboard.log
StandardError=append:$DIR/logs/dashboard.log
EOF
  cat > "$UNIT_DIR/$SVC.timer" <<EOF
[Unit]
Description=usage-coach dashboard timer

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
Unit=$SVC.service

[Install]
WantedBy=timers.target
EOF
  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    systemctl --user daemon-reload
    systemctl --user enable --now "$SVC.timer"
    loginctl enable-linger "$USER" 2>/dev/null || true
  fi
  echo "installed: $UNIT_DIR/$SVC.timer (every five minutes, systemd --user)"
  exit 0
fi

LABEL="com.usage-coach.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
# Bakes in the full PATH as it stands at install time — launchd's default PATH has
# no /usr/local/bin and the like, so coach.py's codexbar call dies with Errno 2
# (measured end to end, 2026-08-04)
PATH_ESC="${PATH//&/&amp;}"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PYTHON_BIN</string>
    <string>$DIR/discord_dash.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH_ESC</string></dict>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
EOF

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  UID_N=$(id -u)
  launchctl bootout "gui/$UID_N/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" "$PLIST"
fi
echo "installed: $PLIST (every five minutes)"
