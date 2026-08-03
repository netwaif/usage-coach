#!/usr/bin/env bash
# usage-coach 대시보드 LaunchAgent 제거 — install.sh 의 대응 제거 경로
set -euo pipefail
LABEL="com.usage-coach.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [[ "${DRY_RUN:-0}" != "1" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
fi
rm -f "$PLIST"
echo "제거됨: $PLIST (설정 ~/.config/usage-coach/ 은 보존)"
