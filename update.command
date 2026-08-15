#!/bin/bash
# Double-click this file (Mac) to update the app to the latest version.
cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "=== Updating inventory app ==="
echo

fail() {
  echo
  echo "$1"
  read -p "Press Enter to close..."
  exit 1
}

command -v git >/dev/null 2>&1 || fail "git isn't available on this computer — can't check for updates this way."

[ -d ".git" ] || fail "This folder isn't set up to receive updates automatically.
Ask whoever set up this app to re-copy the latest version of the folder instead."

git remote get-url origin >/dev/null 2>&1 || fail "No update source is configured yet for this copy of the app.
Ask whoever set up this app to configure one."

echo "Checking for updates..."
git fetch origin || fail "Couldn't reach the update source — check your internet connection and try again."

BRANCH="main"
git rev-parse --verify "origin/$BRANCH" >/dev/null 2>&1 || fail "The update source doesn't have the expected '$BRANCH' branch. Ask whoever set up this app to check it."

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
if [ "$LOCAL" = "$REMOTE" ]; then
  echo
  echo "Already up to date."
  read -p "Press Enter to close..."
  exit 0
fi

echo
echo "Updating to the latest version..."
# Matches the latest version exactly — this folder is only ever a deployed
# copy, never edited by hand, so there's nothing in the tracked files worth
# preserving. Your actual data (db/, uploads/, output/) is never tracked by
# git, so none of it is touched by this, no matter what.
git reset --hard "origin/$BRANCH" || fail "Something went wrong updating the code — see the error above."

echo
command -v uv >/dev/null 2>&1 || fail "uv isn't installed yet — double-click install.command first, then try updating again."

echo "Installing any new dependencies..."
uv sync || fail "Something went wrong installing dependencies — see the error above."

echo
echo "=== Update complete ==="
echo "Double-click run.command to use the app."
read -p "Press Enter to close..."
