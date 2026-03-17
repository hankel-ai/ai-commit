#!/usr/bin/env bash
# macOS launcher for AI Commit Monitor GUI
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/.venv/bin/python3" "$DIR/ai-commit-gui.py" "$@" --url http://192.168.1.65:11434
