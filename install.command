#!/bin/bash
# One-time setup (Mac). Double-click this file, then double-click run.command
# whenever you want to use the app.
set -e
cd "$(dirname "$0")"

echo "=== Inventory app setup ==="
echo

# uv installs to ~/.local/bin (or ~/.cargo/bin on older installs). The
# installer updates shell profiles for *future* terminal sessions, but this
# script's current session won't see that until we add it ourselves.
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if command -v uv >/dev/null 2>&1; then
  echo "uv is already installed ($(uv --version))."
else
  echo "Installing uv (the tool that runs this app)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    echo
    echo "uv installed, but this window can't find it yet. Close this window,"
    echo "open a new Terminal, and run this script again — that's usually enough."
    read -p "Press Enter to close..."
    exit 1
  fi
  echo "uv installed: $(uv --version)"
fi

echo
echo "Installing app dependencies..."
uv sync

echo
echo "=== Setup complete ==="
echo "Double-click run.command any time you want to open the app."
read -p "Press Enter to close..."
