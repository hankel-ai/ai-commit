# ai-commit

AI-powered git commit message generator with GUI and CLI interfaces.

## Tech Stack

- **Language:** Python 3.7+
- **GUI:** Dear PyGui 2.0+ (`dearpygui`)
- **System Tray:** pystray 0.19+
- **Icons:** Pillow 10.0+
- **Platform:** Windows 11 (primary), macOS/Linux supported

## AI Providers

- **Ollama** (default) — HTTP API to local/remote Ollama instance (`/api/chat`)
- **Kiro** — Via WSL `kiro-cli` command

Default model: `qwen3-coder:480b-cloud` (configurable via settings or `AI_COMMIT_MODEL` env var)

## Key Files

| File | Purpose |
|------|---------|
| `ai-commit-gui.py` | GUI app (Dear PyGui) — monitors repos, generates messages, commit & push |
| `ai-commit.py` | CLI wrapper for single-repo commit generation |
| `ai_commit_core.py` | Shared logic: git ops, diff generation, AI provider calls, config defaults |
| `ai-commit-gui-settings.json` | Persisted GUI settings (window pos, provider, model, watched folders) |
| `requirements.txt` | Python dependencies |

## Run / Build

```bash
# Install deps
pip install -r requirements.txt

# Run GUI
python ai-commit-gui.py [folder...]

# Run CLI
python ai-commit.py [folder] [--provider ollama] [--model qwen3-coder:480b-cloud]
```

## Architecture

- GUI runs a polling loop that discovers git repos in watched folders and checks for uncommitted changes
- Background tasks (generate, commit+push, pull, poll) run in a `ThreadPoolExecutor` and post results to a `queue.Queue`
- Main thread processes the queue each frame and updates Dear PyGui widgets
- `RepoState` dataclass tracks per-repo UI state (tags, entries, status, messages)
- Settings persist to `ai-commit-gui-settings.json` in project root

## Conventions

- Commit messages follow `type(scope): description` format
- Provider/model defaults are set in `ai_commit_core.default_config()` and mirrored in CLI arg parsers
- Windows-specific code uses `ctypes` for Win32 API (DWM, window positioning)
- macOS uses `AppKit` via `objc` bridge
