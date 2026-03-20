"""Shared logic for AI commit message generation — used by both CLI and GUI."""

import json
import os
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


def get_sync_status(cwd):
    """Fetch from origin and return (ahead, behind) commit counts vs tracking branch.

    Returns (0, 0) if there is no remote or no tracking branch.
    """
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
        return body["message"]["content"].strip()
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


def generate_message(diff, config):
    """Dispatch to the configured AI provider (only Ollama for now)."""
    return generate_message_ollama(diff, config["model"], config["url"])


# ---------------------------------------------------------------------------
# Commit & push
# ---------------------------------------------------------------------------

def do_commit_and_push(cwd, message):
    """Stage all changes, commit, and attempt to push.

    Returns (success: bool, detail: str).
    """
    # Stage
    rc, _, stderr = run_git(["add", "-A"], cwd=cwd)
    if rc != 0:
        return False, f"git add failed: {stderr}"

    # Commit
    rc, stdout, stderr = run_git(["commit", "-m", message], cwd=cwd)
    if rc != 0:
        return False, f"git commit failed: {stderr}"

    detail = stdout.strip()

    # Push
    rc, push_out, push_err = run_git(["push"], cwd=cwd)
    if rc != 0:
        detail += "\nPush failed (no remote or network issue). Commit saved locally."
        detail += f"\n{push_err.strip()}"
    else:
        detail += "\nPushed successfully."

    return True, detail


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
        "model": os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
        "url": os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
    }
