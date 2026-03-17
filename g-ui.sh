#!/usr/bin/env bash
# macOS launcher for AI Commit Monitor GUI
DIR="$(cd "$(dirname "$0")" && pwd)"

# Auto-detach: re-launch in background unless --no-detach is passed
case " $* " in
  *" --no-detach "*)  ;;
  *)
    nohup "$0" --no-detach "$@" &>/dev/null &
    exit 0
    ;;
esac

exec "$DIR/.venv/bin/python3" "$DIR/ai-commit-gui.py" "$@" --url http://192.168.1.65:11434
