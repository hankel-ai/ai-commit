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
| `gh_workflows.py` | GitHub Actions API client: run detection, job/step polling, log fetching, run cancellation |
| `gh_workflow_viewer.py` | Standalone workflow viewer (separate OS window, launched as subprocess via `pythonw.exe`) |
| `ai-commit-gui-settings.json` | Persisted GUI settings (window pos, provider, model, ollama_url, watched folders) |
| `requirements.txt` | Python dependencies |

## Deploy

`deploy.cmd` robocopies the project to `%USERPROFILE%\OneDrive\Programs\ai-commit` (where the startup shortcut points). **The user runs this manually — never invoke it from Claude Code.**

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

- GUI runs a polling loop that discovers git repos **and non-git folders** in watched folders and checks for uncommitted changes
- Background tasks (generate, commit+push, pull, poll) run in a `ThreadPoolExecutor` and post results to a `queue.Queue`
- Main thread processes the queue each frame and updates Dear PyGui widgets
- `RepoState` dataclass tracks per-repo UI state (tags, entries, status, messages); `NonGitFolder` dataclass for non-git directories
- Settings persist to `ai-commit-gui-settings.json` in project root
- **GitHub Actions viewer**: after a successful push, a background thread polls the GitHub API for workflow runs matching the pushed commit SHA (up to 30s). If runs are found, launches `gh_workflow_viewer.py` as a separate OS window (`subprocess.Popen` with `pythonw.exe`). If no runs exist (repo has no workflows, or push didn't trigger any), no window opens. The viewer shows a tabbed UI with one tab per run, collapsible per-step sections with status icons and duration, and per-tab "Open on GitHub" / "Cancel Run" buttons. Logs are fetched per-job as each job completes (GitHub REST API only serves logs for completed jobs), with a final zip download pass to fill in any gaps (e.g. "Post Run" steps). Uses `gh auth token` for authentication.
- Setting `actions_popup_enabled` (default true) toggles the feature; stored in `ai-commit-gui-settings.json`
- **Smart display**: GitHub account only shown in repo header when it differs from active `gh` CLI user; git name/email only shown when it differs from global config
- **MORE panel**: lazy-loaded per-repo panel showing gitignored files, **new-branch creation**, branch switcher, branch deletion, local config removal, and GitHub Actions workflow dispatch
  - **New branch**: input + "Create" runs `git switch -c <name>` off current HEAD and switches to it. Duplicate names are pre-checked against the loaded local-branch list; other invalid names surface git's stderr. Before creating, `bg_create_branch` runs a live `get_status` (not the cached/Paused poll); if the tree is dirty it posts `create_branch_needs_confirm` so the UI warns ("N changes will move to '<name>'") before proceeding. Follows the same fetch→build→callback + two-phase-confirm pattern as Delete branch. Note: Create **carries** uncommitted work (incl. untracked) onto the new branch by design — contrast Switch, which **isolates** it.
  - **Switch branch**: `bg_switch_branch(repo_key, label, args, confirmed=False)` isolates uncommitted work via a branch-tagged autostash. If the source tree is dirty (on a named branch) and unconfirmed, it posts `switch_branch_needs_confirm` first ("N changes on '<source>' will be stashed"). On proceed it runs `git stash push --include-untracked -m ai-commit-autostash:<source>`, then checkout — rolling the stash back if checkout fails. On arrival it pops the topmost `ai-commit-autostash:<target>` stash, but only onto a clean tree and never forcing a conflicted pop (stash kept + status message otherwise). Clean tree / detached HEAD → plain switch, no stash, no prompt. Pure lookup helper `find_autostash_ref` lives in `ai_commit_core.py` (unit-tested in `tests/test_autostash.py`, runnable via `python tests/test_autostash.py`).

## Conventions

- Commit messages follow `type(scope): description` format
- Provider/model defaults are set in `ai_commit_core.default_config()` and mirrored in CLI arg parsers
- Windows-specific code uses `ctypes` for Win32 API (DWM, window positioning)
- macOS uses `AppKit` via `objc` bridge
