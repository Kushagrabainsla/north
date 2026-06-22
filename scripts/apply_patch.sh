#!/bin/bash
# Run this on the TARGET MACHINE (the one with GitHub creds, where you'll commit/push)
# Usage: ./apply_patch.sh /path/to/changes.patch
# Run it from inside your project folder (the same repo, cloned here).

set -e

if [ -z "$1" ]; then
  echo "Usage: ./apply_patch.sh /path/to/changes.patch"
  exit 1
fi

PATCH_FILE="$1"

if [ ! -f "$PATCH_FILE" ]; then
  echo "ERROR: Patch file not found at: $PATCH_FILE"
  exit 1
fi

echo "== Step 1: Checking you're in a git repo =="
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "ERROR: This isn't a git repo. cd into your project folder first, then re-run this script."
  exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
echo "Repo found at: $REPO_ROOT"

echo ""
echo "== Step 2: Current status (should ideally be clean) =="
git status

echo ""
echo "== Step 3: Pulling latest from remote =="
git pull

echo ""
echo "== Step 4: Checking the patch will apply cleanly =="
if ! git apply --check "$PATCH_FILE"; then
  echo ""
  echo "ERROR: Patch does not apply cleanly. This usually means this machine's repo"
  echo "has diverged from what the source machine started from (different commit, conflicting edits)."
  echo "Run 'git log -1' on both machines and compare — they should match."
  echo "Stopping here without making changes."
  exit 1
fi

echo "Patch check passed."

echo ""
echo "== Step 5: Applying patch =="
git apply "$PATCH_FILE"

echo ""
echo "== Step 6: New status (should show the same changes the source machine had) =="
git status

echo ""
echo "✅ Changes applied. Review them with 'git diff', then commit and push, e.g.:"
echo "   git add -A"
echo "   git commit -m \"your message\""
echo "   git push"