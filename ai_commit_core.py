"""Shared logic for AI commit message generation — used by both CLI and GUI."""

import json
import os
import re
import subprocess
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

def run_git(args, cwd):
    """Run a git command and return (returncode, stdout, stderr)."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )
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


def get_status(cwd):
    """Return list of (status_code, filepath) tuples from git status --porcelain."""
    rc, stdout, _ = run_git(["status", "--porcelain"], cwd=cwd)
    if rc != 0:
        return []
    entries = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip()
        filepath = _unquote_path(line[3:])
        entries.append((code, filepath))
    return entries


def get_git_user(cwd):
    """Return 'Name <email>' for the git user configured in this repo."""
    rc_n, name, _ = run_git(["config", "user.name"], cwd=cwd)
    rc_e, email, _ = run_git(["config", "user.email"], cwd=cwd)
    name = name.strip() if rc_n == 0 else ""
    email = email.strip() if rc_e == 0 else ""
    if name and email:
        return f"{name} <{email}>"
    return name or email or ""


def get_git_user_local_override(cwd):
    """Return (local_name, local_email) if the repo has local config overrides.

    Uses ``--local`` so only values set in ``.git/config`` are returned.
    Returns empty strings for values that are not locally overridden.
    """
    rc_n, name, _ = run_git(["config", "--local", "user.name"], cwd=cwd)
    rc_e, email, _ = run_git(["config", "--local", "user.email"], cwd=cwd)
    local_name = name.strip() if rc_n == 0 else ""
    local_email = email.strip() if rc_e == 0 else ""
    return local_name, local_email


def get_git_global_user():
    """Return (global_name, global_email) from ``git config --global``."""
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


def get_last_commit(cwd):
    """Return (subject, short_date) for HEAD, or ("", "")."""
    rc, stdout, _ = run_git(["log", "-1", "--format=%s|%ci"], cwd=cwd)
    if rc != 0 or not stdout.strip():
        return "", ""
    parts = stdout.strip().split("|", 1)
    if len(parts) < 2:
        return parts[0], ""
    subject = parts[0]
    # Parse ISO-ish date "2026-03-14 15:30:00 +0100" -> "Mar 14 15:30"
    date_str = parts[1].strip()
    try:
        from datetime import datetime
        dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
        short_date = dt.strftime("%b %d %I:%M%p").replace("AM", "am").replace("PM", "pm")
    except (ValueError, IndexError):
        short_date = date_str[:16]
    return subject, short_date


def get_sync_status(cwd, fetch=True):
    """Return (ahead, behind) commit counts vs tracking branch.

    When *fetch* is True (default), fetches from origin first.
    Returns (0, 0) if there is no remote or no tracking branch.
    """
    if fetch:
        # Fetch silently — ignore errors (offline, no remote, etc.)
        run_git(["fetch", "--quiet"], cwd=cwd)

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

    # Untracked files -- show their content so the LLM knows what's new
    _, status_out, _ = run_git(["status", "--porcelain"], cwd=cwd)
    for line in status_out.splitlines():
        if line.startswith("??"):
            filepath = _unquote_path(line[3:])
            full = Path(cwd) / filepath
            if full.is_file():
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
                f"Model '{model}' returned empty content — it may have used "
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

        # Pipe file content into kiro-cli stdin (avoids bash arg-length limits)
        bash_cmd = (
            f"cat '{wsl_path}' | "
            f"kiro-cli chat --no-interactive --model {model} 2>/dev/null"
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

    # Push
    rc, push_out, push_err = run_git(["push"], cwd=cwd)
    if rc != 0:
        detail += f"\nPush failed: {push_err.strip()}"
        return True, False, detail

    detail += "\nPushed successfully."
    return True, True, detail


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

def discover_repos(folder):
    """Return git repo paths to monitor.

    If *folder* itself is a repo root, return just that.
    Otherwise scan its direct children for repo roots.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        return []
    if is_git_repo(folder):
        return [folder]
    repos = []
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
