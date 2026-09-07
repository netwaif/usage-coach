#!/usr/bin/env bash
# usage-coach 대시보드 LaunchAgent 제거 — install.sh 의 대응 제거 경로
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
  echo "제거됨: $UNIT_DIR/$SVC.{service,timer} (설정 ~/.config/usage-coach/ 은 보존)"
  exit 0
fi
LABEL="com.usage-coach.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
rm -f "$PLIST"
echo "제거됨: $PLIST (설정 ~/.config/usage-coach/ 은 보존)"
