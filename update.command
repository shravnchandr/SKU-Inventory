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

if ! command -v git >/dev/null 2>&1; then
  echo "git isn't installed yet — installing it now..."
  if command -v brew >/dev/null 2>&1; then
    brew install git || fail "Couldn't install git automatically. Install it yourself (e.g. from https://git-scm.com/download/mac), then run this update again."
  else
    # No Homebrew — Apple's own Command Line Tools installer includes git
    # and needs no other tool to bootstrap, but it's a GUI dialog + a
    # background download there's no clean way to wait on from a script.
    echo "A software install window should open shortly — click Install there, wait for it"
    echo "to finish (a few minutes), then double-click update.command again."
    xcode-select --install
    read -p "Press Enter to close..."
    exit 1
  fi
  command -v git >/dev/null 2>&1 || fail "git still isn't available after installing — install it yourself (e.g. from https://git-scm.com/download/mac), then run this update again."
  echo "git installed: $(git --version)"
fi

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
