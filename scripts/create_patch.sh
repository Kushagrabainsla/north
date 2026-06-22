#!/bin/bash
# Run this on the SOURCE MACHINE (the one with the changes you want to move)
# Usage: ./create_patch.sh
# Run it from inside your project folder, or it will ask you to cd there first.

set -e

echo "== Step 1: Checking you're in a git repo =="
if ! git rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "ERROR: This isn't a git repo. cd into your project folder first, then re-run this script."
  exit 1
fi

REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"
echo "Repo found at: $REPO_ROOT"

echo ""
echo "== Step 2: Current status =="
git status

echo ""
echo "== Step 3: Staging all changes (including new files) =="
git add -A

echo ""
echo "== Step 4: Creating patch file =="
PATCH_NAME="changes.patch"
git diff --cached > "$PATCH_NAME"

echo ""
echo "== Step 5: Unstaging again (so this machine's working tree is untouched) =="
git reset

if [ -s "$PATCH_NAME" ]; then
  echo ""
  echo "✅ Done. Patch created at: $REPO_ROOT/$PATCH_NAME"
  echo "Now AirDrop or transfer this file to the other machine:"
  echo "   $REPO_ROOT/$PATCH_NAME"
  echo ""
  echo "Then run apply_patch.sh on the other machine, pointing it at this patch file."
else
  echo ""
  echo "⚠️  Patch file is empty. This usually means there were no changes detected."
  echo "Double check 'git status' showed modified or untracked files before running this."
fi