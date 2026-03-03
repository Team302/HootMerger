#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"

if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  echo "Error: python3 tkinter module is missing."
  echo "Install it, then rebuild:"
  echo "  sudo apt-get update && sudo apt-get install -y python3-tk"
  exit 1
fi

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --windowed \
  --collect-all phoenix6 \
  --name HootMergerGUI \
  hoot_merger_gui.py

echo "Build complete: dist/HootMergerGUI"
