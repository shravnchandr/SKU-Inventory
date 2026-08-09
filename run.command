#!/bin/bash
# Double-click this file (Mac) to start the inventory app.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "Setup hasn't been run yet on this computer."
  echo "Double-click install.command first, then come back to this."
  read -p "Press Enter to close..."
  exit 1
fi

uv run app.py
read -p "Press Enter to close..."
