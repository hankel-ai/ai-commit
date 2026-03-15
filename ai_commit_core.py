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
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def is_git_repo(path):
    rc, _, _ = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return rc == 0


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
        filepath = line[3:]
        entries.append((code, filepath))
    return entries


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
            filepath = line[3:]
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
    """Scan direct children of *folder* and return paths that are git repos."""
    folder = Path(folder)
    repos = []
    if not folder.is_dir():
        return repos
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
