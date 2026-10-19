#!/usr/bin/env bash
# Turn on north's nightly check of itself, run in this project folder: lint, the
# whole suite, and the live Claude Code and Codex tests (scripts/smoke.sh).
#
#   scripts/install_smoke_schedule.sh            # turn on, or point it at this folder
#   scripts/install_smoke_schedule.sh --remove   # turn off
#
# north runs it, on the "Nightly check" schedule at 03:30, so the north server must
# be running. This used to install a launchd job instead. macOS keeps launchd jobs
# out of the Desktop, Documents and Downloads folders, so with the project on the
# Desktop it ended "Operation not permitted" every night from Oct 7, and the failure
# it exists to report was never reported. A laptop asleep at 03:30 is checked as soon
# as it is awake.

set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
smoke="${NORTH_HOME:-$HOME/.north}/smoke"
label="com.north.smoke"
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$smoke"

# The old launchd job, if there is one: stopped, and its file kept beside the reports.
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
if [ -f "$plist" ]; then
    mv "$plist" "$smoke/$label.plist.removed"
    echo "stopped the old launchd job; its file is kept at $smoke/$label.plist.removed"
fi

scheduled() { north cron list 2>/dev/null | grep -q "nightly_check"; }

if [ "${1:-}" = "--remove" ]; then
    rm -f "$smoke/checkout"
    if scheduled; then north cron rm nightly_check; fi
    echo "nightly check turned off"
    exit 0
fi

printf '%s\n' "$repo" >"$smoke/checkout"
if ! scheduled; then
    north cron add nightly-check --hour 3 --minute 30 --name nightly_check --label "Nightly check"
fi
echo "nightly check on: north checks $repo every night at 03:30"
