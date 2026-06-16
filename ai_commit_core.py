"""Shared logic for AI commit message generation — used by both CLI and GUI."""

import fnmatch
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


SYSTEM_PROMPT = (
    "You are a git commit message generator. Write a concise commit message "
    "for the following changes. Use conventional commit format:\n"
    "- First line: type(scope): description (max 72 chars)\n"
    "- Types: feat, fix, refactor, docs, style, test, chore, build\n"
    "- Optional body after blank line for non-trivial changes\n"
    "Return ONLY the commit message, no extra commentary."
)

MAX_DIFF_CHARS = 8000

# Untracked files whose basename matches any of these patterns have their
# content withheld from the LLM prompt — the diff sent to the AI provider may
# leave the machine (e.g. Ollama cloud models), and files like these tend to
# hold credentials that aren't gitignored yet.
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.jks",
    "*.keystore", "*.kdbx", "*.tfvars", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "credentials", "credentials.*", "*secret*", ".netrc", ".npmrc", ".pypirc",
    "*.htpasswd", "kubeconfig", "*.kubeconfig",
)


def is_sensitive_filename(filepath):
    """True if the file's basename matches a known secret-bearing pattern."""
    name = Path(filepath).name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_FILE_PATTERNS)

STATUS_LABELS = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "??": "untracked", "MM": "modified", "AM": "added",
    "UU": "conflict",
}


class OllamaError(Exception):
    """Raised when Ollama API call fails."""
    pass


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

# Optional callback invoked after every git command, for the activity log.
# Signature: fn(args, cwd, rc, duration_ms, stderr). Left unset by default so the
# core stays dependency-light and unit-testable without the GUI; the GUI installs
# it at startup via activity_log.install_git_logger().
_GIT_LOGGER = None


def set_git_logger(fn):
    """Register (or clear, with None) the per-command git logging callback."""
    global _GIT_LOGGER
    _GIT_LOGGER = fn


# Per-repo serialization. The GUI runs git on a thread pool plus a polling loop,
# so two commands can hit the same repo at once — one then fails on `index.lock`
# and (via get_status returning [] on error) can be misread as a clean tree,
# bypassing the branch-switch confirm gate. A lock per repo dir guarantees only
# one git command runs in a given repo at a time. Locks are leaf-level (run_git
# never calls run_git), so a plain Lock can't deadlock.
_REPO_LOCKS = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _repo_lock(cwd):
    key = os.path.normcase(os.path.abspath(str(cwd))) if cwd else ""
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[key] = lock
        return lock


def run_git(args, cwd):
    """Run a git command and return (returncode, stdout, stderr).

    Serialized per repo dir (see ``_repo_lock``) so concurrent app threads can't
    collide on the same repo's index.
    """
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    start = time.perf_counter()
    with _repo_lock(cwd):
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
    if _GIT_LOGGER is not None:
        try:
            _GIT_LOGGER(
                args, cwd, result.returncode,
                int((time.perf_counter() - start) * 1000),
                result.stderr,
            )
        except Exception:
            pass
    return result.returncode, result.stdout, result.stderr


def is_git_repo(path):
    """Return True only if *path* is the root of a git repository."""
    rc, stdout, _ = run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if rc != 0:
        return False
    return Path(stdout.strip()).resolve() == Path(path).resolve()


def _unquote_path(p):
    """Strip the quotes git adds around paths containing spaces/specials."""
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        return p[1:-1]
    return p


def read_status(cwd):
    """Run ``git status --porcelain`` and return ``(ok, entries)``.

    ``ok`` is False when git itself failed (non-zero exit — e.g. a concurrent op
    held ``index.lock``), which is NOT the same as a clean tree. Callers that
    make destructive decisions (branch switch) must treat ``ok=False`` as
    "unknown" and refuse to proceed, never as "clean".
    """
    rc, stdout, _ = run_git(["status", "--porcelain"], cwd=cwd)
    if rc != 0:
        return False, []
    entries = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip()
        filepath = _unquote_path(line[3:])
        entries.append((code, filepath))
    return True, entries


def get_status(cwd):
    """Return list of (status_code, filepath) tuples from git status --porcelain.

    Returns ``[]`` on git failure (indistinguishable from a clean tree). Callers
    that must tell those two apart should use :func:`read_status` instead.
    """
    _ok, entries = read_status(cwd)
    return entries


def get_git_global_user():
    """Return (global_name, global_email) from ``git config --global``.

    Read once at GUI startup for the toolbar identity label — not per repo,
    so it doesn't flood the activity log.
    """
    rc_n, name, _ = run_git(["config", "--global", "user.name"], cwd=".")
    rc_e, email, _ = run_git(["config", "--global", "user.email"], cwd=".")
    global_name = name.strip() if rc_n == 0 else ""
    global_email = email.strip() if rc_e == 0 else ""
    return global_name, global_email


def get_active_github_account():
    """Return the login name of the currently active gh CLI user, or ''."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, **kwargs,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def get_repo_visibility(cwd):
    """Return 'private' or 'public' for the repo, or '' if unknown."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "visibility", "--jq", ".visibility"],
            cwd=cwd,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, **kwargs,
        )
        if result.returncode == 0:
            return result.stdout.strip().upper()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def get_github_account(remote_url):
    """Extract the GitHub owner/account name from a remote URL.

    E.g. 'https://github.com/hankel-ai/repo.git' -> 'hankel-ai'
    Also handles PAT-embedded URLs and SSH URLs.
    """
    if not remote_url:
        return ""
    url = remote_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # SSH: git@github.com:owner/repo
    if ":" in url and url.startswith("git@"):
        path = url.split(":", 1)[1]
        parts = path.split("/")
        return parts[0] if parts else ""
    # HTTPS (possibly with PAT): https://[token@]github.com/owner/repo
    # Strip scheme
    if "://" in url:
        url = url.split("://", 1)[1]
    # Strip optional token@ prefix
    if "@" in url:
        url = url.split("@", 1)[1]
    # Now: github.com/owner/repo
    parts = url.split("/")
    return parts[1] if len(parts) >= 2 else ""


def get_remote_url(cwd):
    """Return the HTTPS URL for the repo's origin remote, or empty string."""
    rc, stdout, _ = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if rc != 0 or not stdout.strip():
        return ""
    url = stdout.strip()
    # Convert SSH URLs to HTTPS
    if url.startswith("git@"):
        # git@github.com:user/repo.git -> https://github.com/user/repo
        url = url.replace(":", "/", 1).replace("git@", "https://", 1)
    # Strip trailing .git
    if url.endswith(".git"):
        url = url[:-4]
    return url


def get_head_sha(cwd):
    """Return the full SHA of HEAD, or empty string."""
    rc, stdout, _ = run_git(["rev-parse", "HEAD"], cwd=cwd)
    return stdout.strip() if rc == 0 else ""


def get_current_branch(cwd):
    """Return the current branch name, or empty string."""
    rc, stdout, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return stdout.strip() if rc == 0 else ""


def find_autostash_ref(stash_list_output, branch):
    """Return the topmost ``stash@{N}`` ref tagged for *branch*, or None.

    Matches only stashes created by ai-commit's Switch Branch autostash
    (message ``ai-commit-autostash:<branch>``); never manual ``git stash``
    entries. ``git stash list`` orders newest first, so the first match is the
    most recent.
    """
    if not branch or branch == "HEAD":
        return None
    marker = f"ai-commit-autostash:{branch}"
    for line in stash_list_output.splitlines():
        if line.rstrip().endswith(marker):
            return line.split(":", 1)[0].strip()
    return None


def compute_header_open(*, override, paused, has_activity, force_expand,
                        preserve_open, has_prior, prior_open):
    """Decide whether a repo's collapsing header should be open on (re)build.

    Pure decision logic extracted from the GUI so it can be unit-tested.

    ``preserve_open`` is True for *partial* rebuilds (e.g. single-repo refresh,
    refresh-then-generate) where the user's current expand/collapse state must
    survive the rebuild — repos must NOT be auto-collapsed. It is False for
    *full* refreshes (the automatic poll loop or a manual Refresh-all), which
    are the only cases where the activity-based default is (re)applied.

    Precedence:
      1. force-pause override → always collapsed (explicit user setting)
      2. globally paused (unless force-active or force-expand) → collapsed
      3. preserve_open with a known prior state → keep the prior state
      4. otherwise → open iff the repo has activity
    """
    if override == "pause":
        return False
    if paused and override != "active" and not force_expand:
        return False
    if preserve_open and not force_expand and has_prior:
        return prior_open
    return has_activity


def get_last_commit(cwd):
    """Return (full_message, short_date) for HEAD, or ("", "").

    One ``git log`` call: ``%ci`` (a fixed single-line date) on the first line,
    then ``%B`` (the free-form, possibly multi-line message). Split on only the
    first newline so the body's own newlines never collide with the date.
    """
    rc, out, _ = run_git(["log", "-1", "--format=%ci%n%B"], cwd=cwd)
    if rc != 0:
        return "", ""
    date_str, _, msg_out = out.partition("\n")
    date_str = date_str.strip()
    short_date = ""
    if date_str:
        try:
            from datetime import datetime
            dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
            short_date = dt.strftime("%b %d %I:%M%p").replace("AM", "am").replace("PM", "pm")
        except (ValueError, IndexError):
            short_date = date_str[:16]
    return msg_out.strip(), short_date


def get_sync_status(cwd, fetch=True):
    """Return (ahead, behind) commit counts vs tracking branch.

    When *fetch* is True (default), fetches from origin first.
    Returns (0, 0) if there is no remote or no tracking branch.
    """
    if fetch:
        # Fetch silently — ignore errors (offline, no remote, etc.).
        # --prune drops stale remote-tracking refs so branch_status detection
        # ("stale" vs "local only" vs synced) stays accurate.
        run_git(["fetch", "--prune", "--quiet"], cwd=cwd)

    # Find the upstream tracking branch
    rc, upstream, _ = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd)
    if rc != 0 or not upstream.strip():
        return 0, 0

    # Count commits ahead/behind
    rc, stdout, _ = run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream.strip()}"], cwd=cwd)
    if rc != 0 or not stdout.strip():
        return 0, 0
    parts = stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def get_branch_classification(cwd):
    """Return dict of local branch name -> "", "local only", or "stale".

    "local only" -> branch has no upstream configured.
    "stale"      -> branch has an upstream, but the upstream ref no longer
                    exists locally (i.e. remote branch was deleted and the
                    last fetch ran with --prune).
    ""           -> upstream exists; branch is in sync (ahead/behind aside).
    """
    # Skip classification entirely for repos with no remote configured —
    # the header already shows "LOCAL", so per-branch "(local only)" is noise.
    rc, remotes_out, _ = run_git(["remote"], cwd=cwd)
    if rc != 0 or not remotes_out.strip():
        return {}

    rc, stdout, _ = run_git(
        ["for-each-ref", "--format=%(refname:short)|%(upstream:short)", "refs/heads/"],
        cwd=cwd,
    )
    if rc != 0:
        return {}

    rc2, stdout2, _ = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/"],
        cwd=cwd,
    )
    live_remote_refs = {
        line.strip() for line in stdout2.splitlines() if line.strip()
    } if rc2 == 0 else set()

    result = {}
    for line in stdout.splitlines():
        if "|" not in line:
            continue
        name, upstream = line.split("|", 1)
        name = name.strip()
        upstream = upstream.strip()
        if not name:
            continue
        if not upstream:
            result[name] = "local only"
        elif upstream not in live_remote_refs:
            result[name] = "stale"
        else:
            result[name] = ""
    return result


def get_incoming_changes(cwd):
    """Return a summary of commits that a pull would bring in.

    Assumes fetch has already been done (get_sync_status does this).
    Returns (commits_text, diffstat_text) where each is a string.
    commits_text has one line per incoming commit.
    diffstat_text is the --stat output of the diff.
    Returns ("", "") if there is nothing incoming or no upstream.
    """
    # Find upstream
    rc, upstream, _ = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd
    )
    if rc != 0 or not upstream.strip():
        return "", ""
    upstream = upstream.strip()

    # Incoming commits: HEAD..upstream
    rc, commits_out, _ = run_git(
        ["log", "--oneline", "--no-decorate", f"HEAD..{upstream}"], cwd=cwd
    )
    commits_text = commits_out.strip() if rc == 0 else ""

    # Diffstat: what files would change
    rc, stat_out, _ = run_git(
        ["diff", "--stat", f"HEAD...{upstream}"], cwd=cwd
    )
    diffstat_text = stat_out.strip() if rc == 0 else ""

    return commits_text, diffstat_text


def do_pull(cwd):
    """Run git pull. Returns (success: bool, detail: str)."""
    rc, stdout, stderr = run_git(["pull"], cwd=cwd)
    if rc != 0:
        return False, f"git pull failed: {stderr.strip()}"
    return True, stdout.strip()


def get_diff(cwd):
    """Build a combined diff string: staged + unstaged changes and untracked file contents."""
    parts = []

    # Diff of tracked files against HEAD (staged + unstaged)
    rc, stdout, _ = run_git(["diff", "HEAD"], cwd=cwd)
    if rc != 0:
        # Possibly no commits yet -- try diff of staged files
        _, stdout, _ = run_git(["diff", "--cached"], cwd=cwd)
    if stdout.strip():
        parts.append(stdout)

    # Untracked files -- show their content so the LLM knows what's new.
    # Use ls-files so untracked *directories* are expanded into individual
    # files; `status --porcelain` collapses them to a single "?? dir/" line.
    _, untracked_out, _ = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd,
    )
    for filepath in untracked_out.split("\0"):
        if not filepath:
            continue
        full = Path(cwd) / filepath
        # Never follow symlinks — a link pointing outside the repo would leak
        # its target's content into the prompt.
        if full.is_symlink():
            parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new symlink, content not included)")
            continue
        if full.is_file():
            if is_sensitive_filename(filepath):
                parts.append(
                    f"--- /dev/null\n+++ b/{filepath}\n"
                    "(new file, content withheld - filename matches secret pattern)"
                )
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new file)\n{content}")
            except OSError:
                parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new file, unreadable)")

    combined = "\n".join(parts)
    if len(combined) > MAX_DIFF_CHARS:
        combined = combined[:MAX_DIFF_CHARS] + "\n\n[truncated -- diff too large]"
    return combined


# ---------------------------------------------------------------------------
# AI provider
# ---------------------------------------------------------------------------

def generate_message_ollama(diff, model, base_url):
    """Call Ollama chat API and return the generated commit message.

    Raises OllamaError on any failure instead of calling sys.exit().
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": diff},
        ],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["message"]["content"]
        if not content:
            raise OllamaError(
                f"Model '{model}' returned empty content - it may have used "
                "thinking/tool-call mode instead of a plain text response."
            )
        return content.strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise OllamaError(
                f"Model '{model}' not found on Ollama.\n"
                f"Pull it with: ollama pull {model}"
            ) from exc
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Could not reach Ollama at {base_url}\n"
            f"Is Ollama running? Start it with: ollama serve\n({exc})"
        ) from exc
    except (KeyError, json.JSONDecodeError) as exc:
        raise OllamaError(f"Unexpected response from Ollama: {exc}") from exc


class KiroCliError(Exception):
    """Raised when kiro-cli call fails."""
    pass


def generate_message_kiro(diff, model):
    """Call kiro-cli via WSL and return the generated commit message.

    Writes the prompt to a temp file and pipes it via stdin to kiro-cli
    to avoid shell escaping and argument-length issues.
    Raises KiroCliError on any failure.
    """
    import tempfile

    prompt = SYSTEM_PROMPT + "\n\n" + diff

    # Write prompt to a temp file on the Windows side
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    try:
        tmp.write(prompt)
        tmp.close()

        # Convert Windows path to WSL path
        win_path = tmp.name.replace("\\", "/")
        if len(win_path) >= 2 and win_path[1] == ":":
            wsl_path = f"/mnt/{win_path[0].lower()}{win_path[2:]}"
        else:
            wsl_path = win_path

        # Pipe file content into kiro-cli stdin (avoids bash arg-length limits).
        # shlex.quote both values — they end up inside a bash -lc string, so an
        # unquoted model name from settings/env could inject shell commands.
        bash_cmd = (
            f"cat {shlex.quote(wsl_path)} | "
            f"kiro-cli chat --no-interactive --model {shlex.quote(model)} 2>/dev/null"
        )
        cmd = ["wsl", "--", "bash", "-lc", bash_cmd]

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                **kwargs,
            )
        except FileNotFoundError:
            raise KiroCliError(
                "Could not find 'wsl' command.\n"
                "Ensure WSL is installed and kiro-cli is available inside it."
            )
        except subprocess.TimeoutExpired:
            raise KiroCliError("kiro-cli timed out after 180 seconds.")
    finally:
        os.unlink(tmp.name)

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise KiroCliError(f"kiro-cli exited with code {result.returncode}:\n{err}")

    content = result.stdout.strip()
    if not content:
        raise KiroCliError("kiro-cli returned empty output.")
    # Strip ANSI escape codes from kiro-cli's colored output
    content = re.sub(r"\x1b\[[0-9;]*m", "", content)
    # Strip the leading "> " prefix kiro-cli adds to responses
    lines = content.splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r"^>\s?", "", line)
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def generate_message(diff, config):
    """Dispatch to the configured AI provider."""
    provider = config.get("provider", "ollama")
    if provider == "kiro":
        return generate_message_kiro(diff, config["model"])
    return generate_message_ollama(diff, config["model"], config["url"])


# ---------------------------------------------------------------------------
# Commit & push
# ---------------------------------------------------------------------------

def do_commit_and_push(cwd, message):
    """Stage all changes, commit, and attempt to push.

    Returns (committed: bool, pushed: bool, detail: str).
    """
    # Stage
    rc, _, stderr = run_git(["add", "-A"], cwd=cwd)
    if rc != 0:
        return False, False, f"git add failed: {stderr}"

    # Commit
    rc, stdout, stderr = run_git(["commit", "-m", message], cwd=cwd)
    if rc != 0:
        return False, False, f"git commit failed: {stderr}"

    detail = stdout.strip()

    # Push — skip if no remote configured
    rc_remote, _, _ = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if rc_remote != 0:
        return True, False, "LOCAL_ONLY"

    rc, push_out, push_err = run_git(["push"], cwd=cwd)
    if rc != 0:
        if "has no upstream branch" in push_err or "no upstream branch" in push_err:
            branch = get_current_branch(cwd)
            return True, False, f"NO_UPSTREAM:{branch}"
        detail += f"\nPush failed: {push_err.strip()}"
        return True, False, detail

    detail += "\nPushed successfully."
    return True, True, detail


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

def discover_repos(folder):
    """Return git repo paths to monitor.

    Always include *folder* itself if it is a repo root, plus any direct
    child directories that are repo roots. Supports the "umbrella" pattern
    where a folder is its own repo (for loose files) and also contains
    independent child repos.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        return []
    repos = []
    if is_git_repo(folder):
        repos.append(folder)
    for child in sorted(folder.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            if is_git_repo(child):
                repos.append(child)
    return repos


def default_config():
    """Return default config dict respecting env vars."""
    return {
        "provider": os.environ.get("AI_COMMIT_PROVIDER", "ollama"),
        "model": os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
        "url": os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
    }
