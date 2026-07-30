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
| `activity_log.py` | Dependency-light logging core: JSONL log file + in-memory ring buffer, `run_git` hook, `FileTailer` (all stdlib, unit-tested) |
| `activity_log_viewer.py` | Standalone **Activity Log** window (separate OS process) that tails the JSONL log live; launched from the "Activity Log" toolbar button |
| `ai-commit-gui-settings.json` | Persisted GUI settings (window pos, provider, model, ollama_url, watched folders) |
| `requirements.txt` | Python dependencies |

## Deploy

`deploy.cmd` robocopies the project to `%USERPROFILE%\OneDrive\Programs\ai-commit` (where the startup shortcut points). **The user runs this manually — never invoke it from Claude Code.**

## Run / Build

Launchers use an **isolated venv OUTSIDE OneDrive** at `%USERPROFILE%\.venvs\ai-commit`
(avoids OneDrive sync churn). `g-ui.cmd` / `g.cmd` self-bootstrap it on first run
(create venv + `pip install -r requirements.txt`) and self-heal if a dep goes missing.
Never rely on system Python — a July 2026 global-Python reinstall wiped its packages
(see `python312-reinstall-wiped-global-packages` memory).

```bat
g-ui.cmd [folder...]   :: GUI (launches venv pythonw, windowless)
g.cmd [folder] [--provider ollama] [--model qwen3-coder:480b-cloud]   :: CLI
```

**Autostart:** the GUI auto-starts via HKCU `...\Run\AICommitMonitor`, which points
**directly** at `%USERPROFILE%\.venvs\ai-commit\Scripts\pythonw.exe ...\Programs\ai-commit\ai-commit-gui.py`
(bypasses `g-ui.cmd`, so the venv must already exist — run `g-ui.cmd` once after any venv wipe).

Raw (venv must exist): `%USERPROFILE%\.venvs\ai-commit\Scripts\python.exe ai-commit-gui.py`

## Architecture

- GUI runs a polling loop that discovers git repos **and non-git folders** in watched folders and checks for uncommitted changes
  - **Folded poll path** (perf): each repo's live poll runs a single `git status --porcelain --branch` via `ai_commit_core.read_status_branch` + `parse_branch_header`, which yields dirty entries, current branch, ahead/behind, and the branch's local-only/stale classification in one spawn (was 4 separate git calls; ~4.7× faster — 9→2 spawns/repo/cycle). The GUI helper `_read_poll_status(rp, remote_url, do_fetch)` wraps it (optionally `fetch_remote` first) and feeds all three poll sites (`_poll_one_repo`, `bg_refresh_single_repo`, `bg_refresh_then_generate`). Covered by `tests/test_status_branch.py`. **Full analysis + the 5 considered options (incl. the deferred FS-watcher ideas) live in `docs/polling-performance.md`** — read that before further poll-perf work.
  - **Paused fast-path**: when globally paused (and not a manual Refresh), `bg_poll_repos` skips the folder rescan entirely — it iterates only the already-known `app.repos`, runs a live `_poll_one_repo` for force-active repos and reuses `_cached_repo_result` for the rest. Avoids a `git rev-parse` (via `is_git_repo`) on every repo each cycle just to rediscover them. New repos surface on Unpause or Refresh. Per-repo poll/cache logic is factored into `_poll_one_repo` / `_cached_repo_result`, shared with the normal discovery loop. Covered by `tests/test_poll_pause.py`.
- **Recency tier (visibility + poll cost)**: repos are classified Active vs Idle by the pure `ai_commit_core.is_repo_active(commit_ts, dirty, ahead, behind, now, recent_days)` — Active = dirty OR ahead/behind OR last commit within `recent_days`. **Active** repos poll every `poll_interval` (120s) and show; **Idle** repos (clean, synced, old) are hidden by the recency filter and polled only every `idle_poll_interval` (default 900s). The tiered gate lives in `bg_poll_repos`' normal loop: a known Idle repo whose last live poll (`app.idle_last_poll[repo_key]`, stamped on **every** live poll) is within `idle_poll_interval` reuses `_cached_repo_result`; new repos, manual **Refresh** (`force=True`), and force-active overrides always poll live. Display filter is applied in `rebuild_repos_ui`'s render loop (skips `build_repo_section` for Idle repos unless force-active or sticky-error; sets `hidden_count_label`); the raw poll payload is cached (`app.last_results`/`app.last_non_git`) so toggling **Recent only** re-renders with **no** git work. `get_last_commit` now returns a 3rd value `commit_ts` (epoch) carried through the result dicts / `RepoState.last_commit_ts` / `_cached_repo_result`. **Timezone**: `git log --format=%ci` prints the committer's *own* zone with an offset (`2026-07-20 21:30:00 +0530`); the pure `ai_commit_core.parse_commit_date` honours that offset and re-renders in the **viewer's** local zone. Dropping the offset (the old behaviour) made commits pushed from another zone — e.g. GitLab colleagues in IST, CI runners on UTC — display as *future* timestamps and skewed `commit_ts` for the recency window. Covered by `tests/test_commit_date.py` (all assertions are tz-independent). Settings: `recent_only` (bool, toolbar + Settings→Display checkboxes), `recent_days` (int, default 14), `idle_poll_interval` (int secs, default 900). **Non-git folders** get the same recency filter via the pure `ai_commit_core.is_folder_recent(mtime, now, recent_days)` — the folder's `stat().st_mtime` (stamped into the poll payload / `NonGitFolder.mtime`) stands in for git signals; unknown mtime (0.0) always shows. Hidden non-git folders count into `hidden_count_label` (only when `show_non_git_folders` is on) and stay in `app.non_git_folders`. This replaces the old manual "move idle repos into a `_idle_projects_*` subfolder" workaround (discovery is still one level deep, so that trick still works too). Covered by `tests/test_recency.py` (pure predicates). See `docs/polling-performance.md` for the poll-cost background.
- **Header expand/collapse**: `ai_commit_core.compute_header_open` is the single pure decision (unit-tested in `tests/test_header_open.py`). Full refreshes (poll loop / Refresh-all) use `preserve_open=False` → activity-based default. Partial rebuilds (single-repo refresh, refresh-then-generate) use `preserve_open=True` → keep each header's prior open state so other repos don't auto-collapse. Two per-repo opt-outs override `preserve_open` for one rebuild: `app.expand_on_next_build` / `force_expand` (force open even when paused, e.g. single-repo Refresh) and `app.collapse_on_next_build` / `force_collapse` (re-apply the activity default so a now-idle repo collapses — set after a successful **Commit & Push** so the just-pushed repo doesn't stay stuck expanded).
- **Sticky errors**: a repo's `GenStatus.ERROR` (e.g. push failure) survives rebuilds (`rebuild_repos_ui` sticky-error branch) so polls don't wipe it. It is cleared by: Refresh-all (`poll_result` plumbs `force` → `clear_errors`), a force-active repo's poll, or a **manual per-repo Refresh** — `bg_refresh_single_repo(force=True)` carries `force` in the `single_repo_refresh` queue message and the handler resets that one repo's error before rebuilding. Automatic single-repo refreshes (post-commit, branch ops, `force=False`) never clear errors. Covered by `tests/test_single_refresh_error.py`.
- **Secret-push-protection override**: when a push is rejected by GitLab's secret push protection (pre-receive prints "PUSH BLOCKED: Secrets detected" + suggests `-o secret_push_protection.skip_all`), `is_secret_push_block` (in `ai_commit_core`) detects it in both failure paths (`commit_result` push error and `push_upstream_result`) and auto-shows `_show_secret_push_prompt` — a red-warning popup offering "Push Anyway (skip protection)". Confirm runs `bg_push_override` (`git push -o secret_push_protection.skip_all`, plus `--set-upstream origin <branch>` if the blocked push was the set-upstream one; `push_upstream_result` messages carry the branch as an optional 5th element). Cancel leaves the sticky error as-is. Result is posted as `push_upstream_result`, reusing its success flow (status, collapse, refresh, Actions viewer). Covered by `tests/test_secret_push_override.py`.
- **EOL-only changes (dirty status, empty diff)**: with git check-in normalization on (`core.autocrlf=input`/`true`, or a `* text=auto` .gitattributes rule), a file whose only change is CRLF↔LF is listed by `git status --porcelain` as ` M` but yields an **empty** `git diff HEAD` / `--stat` / `--name-only` — git converts the working copy back to the committed blob, so `git commit` would say "nothing to commit". `get_diff` therefore returned `""` and Generate reported a bare "No diff content available."; `ai_commit_core.describe_empty_diff(cwd, only_path=None)` now explains it instead (which file, `LF committed, CRLF on disk`, the effective `core.autocrlf`, and the fix: mark the path `-text` in `.gitattributes` + `git add --renormalize .` — verified that `text eol=lf` does **not** change the blob, it only controls checkout). Used by `bg_generate_message` and `bg_launch_diff_viewer`. Detection = `normalizes_to_same_content` (covers the byte-identical `autocrlf=true` case, where the blob is LF and only the expected *checkout* is CRLF) **plus** an empty per-file `git diff HEAD --` guard so mode-only changes aren't mislabelled. Reading blobs needs `run_git_bytes` — plain `run_git` is text-mode, and universal newlines would rewrite the very CRLFs being inspected (`run_git`/`run_git_bytes` now share `_run_git`). The repo status line is `wrap=0` so the multi-line explanation wraps. Covered by `tests/test_eol_only.py`.
- **Init on a folder git refuses to read (dubious ownership)**: `git init` returns **0** in a directory whose *owner* isn't the current user, but every command afterwards exits 128 -- including the `rev-parse --show-toplevel` behind `is_git_repo`. The folder therefore re-rendered as "not a git repo" with an Init button, so Init looked like a no-op and stayed clickable forever (seen on `ClaudeCode\cc-tmux`: 3 × `git init` rc=0 against 314 × `rev-parse` rc=128 in the activity log). On Windows a folder created by an **elevated** process is owned by `BUILTIN\Administrators` (SID `S-1-5-32-544`) even when its contents are user-owned; git's safe.directory check only waives an Administrators-owned path when the *reading* process is itself elevated (`IsUserAnAdmin()`), which the GUI never is. `bg_git_init` now calls `ai_commit_core.verify_repo_usable(cwd)` after a zero-exit init and posts the failure instead of a bogus success; `is_dubious_ownership` / `describe_dubious_ownership` turn git's five-line fatal into the two real fixes (`icacls <dir> /setowner "%USERDOMAIN%\%USERNAME%"`, preferred, or a `safe.directory` exception). The non-git status line is `wrap=0` so that explanation wraps. Covered by `tests/test_dubious_ownership.py`. Note this state can't be reproduced from a non-elevated shell -- Windows rejects *assigning* Administrators as an owner ("This security ID may not be assigned as the owner of this object"), so the ownership-refusal path is tested by stubbing `run_git`.
- Background tasks (generate, commit+push, pull, poll) run in a `ThreadPoolExecutor` and post results to a `queue.Queue`
- Main thread processes the queue each frame and updates Dear PyGui widgets
- `RepoState` dataclass tracks per-repo UI state (tags, entries, status, messages); `NonGitFolder` dataclass for non-git directories
- Settings persist to `ai-commit-gui-settings.json` in project root
- **Concurrency safety**: `run_git` serializes per repo dir via a `threading.Lock` registry (`_repo_lock`) — the 4-worker pool + poll loop could otherwise collide on a repo's `index.lock`. `read_status(cwd)` returns `(ok, entries)` so callers can tell a git **failure** (`ok=False`) from a **clean tree**; `get_status` still returns `[]` for both (back-compat). Destructive flows (switch/create branch) use `read_status` and **abort** if `ok=False` rather than treating a failed status as clean — that bug previously let a status read losing an `index.lock` race bypass the confirm gate and switch branches without stashing. Covered by `tests/test_status.py`.
- **GitHub Actions viewer**: after a successful push, a background thread polls the GitHub API for workflow runs matching the pushed commit SHA (up to 30s). If runs are found, launches `gh_workflow_viewer.py` as a separate OS window (`subprocess.Popen` with `pythonw.exe`). If no runs exist (repo has no workflows, or push didn't trigger any), no window opens. The payload (which includes the gh auth token) is piped to the viewer via **stdin** (`argv[1] == "-"`), never written to disk; a temp-file path in `argv[1]` is still accepted as a legacy fallback. The viewer shows a tabbed UI with one tab per run, collapsible per-step sections with status icons and duration, and per-tab "Open on GitHub" / "Cancel Run" buttons. Logs are fetched per-job as each job completes (GitHub REST API only serves logs for completed jobs), with a final zip download pass to fill in any gaps (e.g. "Post Run" steps). Uses `gh auth token` for authentication.
- Setting `actions_popup_enabled` (default true) toggles the feature; stored in `ai-commit-gui-settings.json`
- **Activity Log**: every git command run under the hood flows through `ai_commit_core.run_git`, which calls an optional logger hook (`set_git_logger`). At startup the GUI calls `activity_log.clear_file()` + `install_git_logger()` so all git commands plus high-level events (generate/commit/push/pull) are written as JSON Lines to `%TEMP%/ai-commit-activity.jsonl`. The "Activity Log" toolbar button launches `activity_log_viewer.py` as a separate process that tails that file live (category filters, search, auto-scroll, pause, clear-view, **copy**: right-click any row → "Copy line", or the toolbar "Copy all" button copies all currently-visible/filtered rows as plain text via `dpg.set_clipboard_text`). The core stays decoupled — it only knows about the optional callback, never imports `activity_log`.
- **Smart display**: GitHub account only shown in repo header when it differs from active `gh` CLI user. Your global git identity is shown once in the top toolbar (`get_git_global_user`, a single read at startup). (Per-repo git identity probing — `get_git_user`, the effective-vs-global `!! Using different identity` header warning, and the `--local` override panel — was removed: it ran `git config user.name`/`user.email` on every repo each poll, flooding the activity log for no displayed value.)
- **MORE panel**: lazy-loaded per-repo panel showing gitignored files, **new-branch creation**, branch switcher, branch deletion, and GitHub Actions workflow dispatch
  - **New branch**: input + "Create" runs `git switch -c <name>` off current HEAD and switches to it. Duplicate names are pre-checked against the loaded local-branch list; other invalid names surface git's stderr. Before creating, `bg_create_branch` runs a live `read_status` (not the cached/Paused poll) and aborts if the tree can't be read; if the tree is dirty it posts `create_branch_needs_confirm` so the UI warns ("N changes will move to '<name>'") before proceeding. Follows the same fetch→build→callback + two-phase-confirm pattern as Delete branch. Note: Create **carries** uncommitted work (incl. untracked) onto the new branch by design — contrast Switch, which **isolates** it.
  - **Switch branch**: `bg_switch_branch(repo_key, label, args, confirmed=False)` isolates uncommitted work via a branch-tagged autostash. It reads the tree with `read_status` and **aborts** if git can't be read (never treats a failed status as clean). If the source tree is dirty (on a named branch) and unconfirmed, it posts `switch_branch_needs_confirm` first ("N changes on '<source>' will be stashed"). On proceed it runs `git stash push --include-untracked -m ai-commit-autostash:<source>`, then checkout — rolling the stash back if checkout fails. On arrival it pops the topmost `ai-commit-autostash:<target>` stash, but only onto a clean tree and never forcing a conflicted pop (stash kept + status message otherwise). Clean tree / detached HEAD → plain switch, no stash, no prompt. Pure lookup helper `find_autostash_ref` lives in `ai_commit_core.py` (unit-tested in `tests/test_autostash.py`, runnable via `python tests/test_autostash.py`).

## Security hardening (fix/security-hardening)

- **Diff privacy**: `get_diff` withholds the content of untracked files whose basename matches `SENSITIVE_FILE_PATTERNS` (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `*secret*`, etc. — see `is_sensitive_filename`) and never follows symlinks. The filename still appears in the prompt so the commit message can mention it. Matters because the default model (`qwen3-coder:480b-cloud`) is proxied to Ollama's cloud.
- **Activity log redaction**: `activity_log.redact_credentials` masks URL-embedded credentials (`https://token@host` → `https://***@host`) in every entry's message/detail before buffering/writing — git echoes remote URLs (incl. PATs) in push/fetch errors.
- **Kiro provider**: model name and temp path are `shlex.quote`d before interpolation into the `wsl -- bash -lc` command string (settings/env-supplied model names can't inject shell).
- **gh token**: never written to disk; piped to `gh_workflow_viewer.py` via stdin (see Actions viewer above).
- Tests: `python tests/test_security.py` (symlink test self-skips without symlink privilege).

## Conventions

- Commit messages follow `type(scope): description` format
- Provider/model defaults are set in `ai_commit_core.default_config()` and mirrored in CLI arg parsers
- Windows-specific code uses `ctypes` for Win32 API (DWM, window positioning)
- macOS uses `AppKit` via `objc` bridge
