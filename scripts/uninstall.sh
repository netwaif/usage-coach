#!/usr/bin/env bash
# Removes the usage-coach dashboard's LaunchAgent — the counterpart to install.sh
set -euo pipefail
OS_NAME="${HARNESS_OS:-$(uname -s)}"
if [[ "$OS_NAME" == "Linux" ]]; then
  SVC="usage-coach-dashboard"; UNIT_DIR="$HOME/.config/systemd/user"
  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    systemctl --user disable --now "$SVC.timer" 2>/dev/null || true
    systemctl --user stop "$SVC.service" 2>/dev/null || true
  fi
  rm -f "$UNIT_DIR/$SVC.service" "$UNIT_DIR/$SVC.timer"
  if [[ "${DRY_RUN:-0}" != "1" ]]; then systemctl --user daemon-reload 2>/dev/null || true; fi
  echo "removed: $UNIT_DIR/$SVC.{service,timer} (the config in ~/.config/usage-coach/ is kept)"
  exit 0
fi
LABEL="com.usage-coach.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
rm -f "$PLIST"
echo "removed: $PLIST (the config in ~/.config/usage-coach/ is kept)"
