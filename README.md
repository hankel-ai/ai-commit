# ai-commit

AI-powered git commit message generator using a local [Ollama](https://ollama.com) LLM. Includes both a CLI tool and a GUI monitor.

## How it works

1. Detects uncommitted changes in a git repo
2. Sends the diff to a local Ollama model
3. Generates a [conventional commit](https://www.conventionalcommits.org/) message
4. Lets you accept, regenerate, edit, or abort
5. Runs `git add -A`, `git commit`, and `git push`

## Requirements

- Python 3.7+
- [Ollama](https://ollama.com) running locally (or accessible via network)
- A pulled model (e.g. `ollama pull qwen3-coder:480b-cloud`)

## CLI Usage

```bash
# Run in current directory
python ai-commit.py

# Run against a specific repo
python ai-commit.py /path/to/repo

# Use a different model
python ai-commit.py --model mistral

# Test Ollama connectivity without committing
python ai-commit.py --test

# Show the diff being sent to the LLM
python ai-commit.py --debug
```

## GUI Usage

The GUI monitors all git repos inside a folder and lets you generate/accept/edit commit messages per repo.

### Install dependencies

```bash
pip install -r requirements.txt
```

### Launch

```bash
# Monitor repos in current directory
python ai-commit-gui.py

# Monitor a specific folder
python ai-commit-gui.py C:\Projects

# Custom model and poll interval
python ai-commit-gui.py --model mistral --poll 15
```

### Features

- **Auto-scan**: polls a folder for git repos and shows changed files per repo
- **Per-repo controls**: Generate, Accept & Push, Regenerate buttons for each repo
- **Editable messages**: commit message input is always editable
- **Auto-generate**: toggle to automatically generate messages when changes are detected
- **System tray**: close button hides to tray; right-click tray to Show or Quit
- **Always-on-top**: compact window stays visible while you work
- **Drag-to-move**: custom title bar with drag support
- **GitHub Actions viewer**: after pushing, automatically detects any triggered workflow runs and opens a live status window with per-step logs, run cancellation, and direct GitHub links

## Options

| Flag | Env Var | Default | Description |
|------|---------|---------|-------------|
| `[folder]` | -- | `.` (cwd) | Path to the git repository (CLI) or parent folder (GUI) |
| `--model` | `AI_COMMIT_MODEL` | `qwen3-coder:480b-cloud` | Ollama model name |
| `--url` | `AI_COMMIT_URL` | `http://localhost:11434` | Ollama base URL |
| `--test` | -- | off | Generate message only, don't commit (CLI only) |
| `--debug` | -- | off | Print the full diff sent to the LLM (CLI only) |
| `--poll` | -- | `30` | Poll interval in seconds (GUI only) |

## Interactive prompt (CLI)

After generating a commit message, you'll see:

```
[Enter] Accept   [r] Regenerate   [e] Edit   [q] Quit
```

- **Enter** -- accept the message and run add/commit/push
- **r** -- ask the LLM for a new message
- **e** -- type your own replacement message
- **q** -- abort without changes

## File structure

```
ai_commit_core.py       # Shared logic (git helpers, Ollama API, repo discovery)
ai-commit.py            # CLI wrapper
ai-commit-gui.py        # Dear PyGui GUI application
gh_workflows.py         # GitHub Actions API client (run detection, log fetching)
gh_workflow_viewer.py   # Standalone Actions viewer window (launched as subprocess)
requirements.txt        # GUI dependencies (dearpygui, pystray, Pillow)
```

## Commit format

Messages follow conventional commit style:

```
type(scope): description

Optional body for non-trivial changes.
```

Types: `feat`, `fix`, `refactor`, `docs`, `style`, `test`, `chore`, `build`

## Error handling

- **Ollama not running** -- prints connection error with `ollama serve` hint
- **Model not found** -- prints 404 with `ollama pull` hint
- **Not a git repo** -- prints error and exits
- **No changes** -- prints "nothing to commit"
- **Push fails** -- commit is saved locally, warning printed
- **Large diffs** -- truncated to ~8000 chars to fit LLM context
