#!/usr/bin/env bash
# Run scripts/smoke.sh every night at 03:30, live tests included (macOS launchd).
#
#   scripts/install_smoke_schedule.sh            # install or update
#   scripts/install_smoke_schedule.sh --remove   # uninstall
#
# A Mac asleep at 03:30 runs it on wake. PATH is captured from this shell so the
# job finds `claude` and `codex` the way you do.

set -euo pipefail

label="com.north.smoke"
plist="$HOME/Library/LaunchAgents/$label.plist"
repo="$(cd "$(dirname "$0")/.." && pwd)"
logs="${NORTH_HOME:-$HOME/.north}/smoke"

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
    rm -f "$plist"
    echo "removed $label"
    exit 0
fi

mkdir -p "$logs" "$(dirname "$plist")"
cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array><string>$repo/scripts/smoke.sh</string></array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>SMOKE_LIVE</key><string>1</string>
        <key>PATH</key><string>$PATH</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
    <key>StandardOutPath</key><string>$logs/launchd.out</string>
    <key>StandardErrorPath</key><string>$logs/launchd.err</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "installed $label: nightly at 03:30, reports in $logs"
