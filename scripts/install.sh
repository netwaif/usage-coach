#!/usr/bin/env bash
# usage-coach 대시보드 LaunchAgent 설치 — discord_dash.py 5분 간격 실행
# DRY_RUN=1 이면 launchctl 을 건너뛰고 plist 파일만 만든다(테스트 계약).
set -euo pipefail
fail() { echo "오류: $*" >&2; exit 1; }

DIR="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$(uname)" == "Darwin" ]] || fail "macOS 전용입니다 (launchd 사용)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
[[ -n "$PYTHON_BIN" ]] || fail "python3 를 찾을 수 없음"
[[ -f "$HOME/.config/usage-coach/discord.json" || -n "${DISCORD_WEBHOOK_URL:-}" ]] \
  || fail "webhook_url 미설정 — ~/.config/usage-coach/discord.json 에 {\"webhook_url\": \"...\"} 를 먼저 만드세요"

LABEL="com.usage-coach.dashboard"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
# 설치 시점의 완전한 PATH를 굽는다 — launchd 기본 PATH에는 /usr/local/bin 등이 없어
# coach.py의 codexbar 호출이 Errno 2로 죽는다 (2026-08-04 E2E 실측)
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
echo "설치됨: $PLIST (5분 간격)"
