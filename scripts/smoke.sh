#!/usr/bin/env bash
# The nightly smoke run: is north still working end to end?
#
# Runs lint, the whole suite, and - with SMOKE_LIVE=1 - the live tests against the
# installed `claude` and `codex` (they spend a little of the plan). Each stage's
# output goes to one dated report; the last line of the report is the verdict.
# A failure raises a macOS notification, so a broken night is not found a week later.
#
#   scripts/smoke.sh                 # lint + suite
#   SMOKE_LIVE=1 scripts/smoke.sh    # and the live coding-agent tests
#
# Scheduled by scripts/install_smoke_schedule.sh. Exit code: 0 all passed, 1 a stage failed.

set -u

repo="$(cd "$(dirname "$0")/.." && pwd)"
reports="${NORTH_HOME:-$HOME/.north}/smoke"
mkdir -p "$reports"
report="$reports/$(date +%Y-%m-%d_%H%M).log"
python="$repo/.venv/bin/python"
failed=()

cd "$repo" || exit 1

stage() {
    local name="$1"
    shift
    {
        echo "=== $name ($(date +%H:%M:%S))"
        "$@"
    } >>"$report" 2>&1
    local status=$?
    echo "--- $name: $([ $status -eq 0 ] && echo passed || echo FAILED)" >>"$report"
    [ $status -eq 0 ] || failed+=("$name")
}

echo "north smoke run $(date '+%Y-%m-%d %H:%M') at $(git rev-parse --short HEAD) on $(git branch --show-current)" >"$report"
git status --short >>"$report"

stage "lint" "$repo/.venv/bin/ruff" check .
stage "suite" "$python" -m pytest -q --tb=short -m "not integration" -p no:cacheprovider
if [ "${SMOKE_LIVE:-0}" = "1" ]; then
    stage "live coding agents" env NORTH_LIVE_CLAUDE=1 NORTH_LIVE_CODEX=1 \
        "$python" -m pytest tests/live -q --tb=short -p no:cacheprovider
fi

if [ ${#failed[@]} -eq 0 ]; then
    echo "VERDICT: passed" >>"$report"
    echo "smoke passed: $report"
    exit 0
fi

summary="failed: ${failed[*]}"
echo "VERDICT: $summary" >>"$report"
echo "smoke $summary - $report"
if command -v osascript >/dev/null; then
    osascript -e "display notification \"$summary. Report: $(basename "$report")\" with title \"north smoke run\"" || true
fi
exit 1
