#!/usr/bin/env python3
"""AI Commit Message Generator — uses a local Ollama LLM to write commit messages."""

import argparse
import json
import os
import subprocess
import sys
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
        # Possibly no commits yet — try diff of staged files
        _, stdout, _ = run_git(["diff", "--cached"], cwd=cwd)
    if stdout.strip():
        parts.append(stdout)

    # Untracked files — show their content so the LLM knows what's new
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
        combined = combined[:MAX_DIFF_CHARS] + "\n\n[truncated — diff too large]"
    return combined


def print_change_summary(entries):
    """Print a human-readable summary of changed files."""
    labels = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed",
              "C": "copied", "??": "untracked", "MM": "modified", "AM": "added",
              "UU": "conflict"}
    print("\nChanged files:")
    for code, filepath in entries:
        label = labels.get(code, code)
        print(f"  {label:>10}  {filepath}")
    print()


# ---------------------------------------------------------------------------
# AI provider
# ---------------------------------------------------------------------------

def generate_message_ollama(diff, model, base_url):
    """Call Ollama chat API and return the generated commit message."""
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
            print(f"\nError: Model '{model}' not found on Ollama.")
            print("Available models can be listed with: ollama list")
            print(f"Pull it with: ollama pull {model}")
        else:
            print(f"\nError: Ollama returned HTTP {exc.code}: {exc.reason}")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"\nError: Could not reach Ollama at {base_url}")
        print("Is Ollama running? Start it with: ollama serve")
        print(f"  ({exc})")
        sys.exit(1)
    except (KeyError, json.JSONDecodeError) as exc:
        print(f"\nError: Unexpected response from Ollama — {exc}")
        sys.exit(1)


def generate_message(diff, config):
    """Dispatch to the configured AI provider (only Ollama for now)."""
    return generate_message_ollama(diff, config["model"], config["url"])


# ---------------------------------------------------------------------------
# User interaction
# ---------------------------------------------------------------------------

def prompt_user(message):
    """Show the commit message and let the user accept, regenerate, edit, or quit.

    Returns (action, message) where action is 'accept', 'regenerate', or 'quit'.
    """
    print("─" * 60)
    print("Proposed commit message:\n")
    print(message)
    print("\n" + "─" * 60)
    print("[Enter] Accept   [r] Regenerate   [e] Edit   [q] Quit")

    choice = input("> ").strip().lower()
    if choice in ("", "y", "yes"):
        return "accept", message
    if choice == "r":
        return "regenerate", message
    if choice == "e":
        print("Enter your commit message (end with an empty line):")
        lines = []
        while True:
            line = input()
            if line == "":
                break
            lines.append(line)
        new_msg = "\n".join(lines)
        if not new_msg.strip():
            print("Empty message — aborting.")
            return "quit", message
        return "accept", new_msg
    return "quit", message


# ---------------------------------------------------------------------------
# Commit & push
# ---------------------------------------------------------------------------

def do_commit_and_push(cwd, message):
    """Stage all changes, commit, and attempt to push."""
    # Stage
    rc, _, stderr = run_git(["add", "-A"], cwd=cwd)
    if rc != 0:
        print(f"git add failed: {stderr}")
        return False

    # Commit
    rc, stdout, stderr = run_git(["commit", "-m", message], cwd=cwd)
    if rc != 0:
        print(f"git commit failed: {stderr}")
        return False
    print(stdout)

    # Push
    rc, stdout, stderr = run_git(["push"], cwd=cwd)
    if rc != 0:
        print("Warning: git push failed (no remote configured or network issue).")
        print(f"  {stderr.strip()}")
        print("Your commit was saved locally.")
    else:
        print("Pushed successfully.")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate AI-powered git commit messages using a local Ollama LLM.",
    )
    parser.add_argument(
        "folder", nargs="?", default=".",
        help="Path to the git repository (default: current directory)",
    )
    parser.add_argument(
        "--model", default=os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
        help="Ollama model name (default: qwen3-coder:480b-cloud, env: AI_COMMIT_MODEL)",
    )
    parser.add_argument(
        "--url", default=os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
        help="Ollama base URL (default: http://localhost:11434, env: AI_COMMIT_URL)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Test mode: check Ollama connectivity, generate message, but don't commit",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Show the full git diff sent to the LLM",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    target = Path(args.folder).resolve()
    config = {"model": args.model, "url": args.url}
    test_mode = args.test

    # Validate target
    if not target.is_dir():
        print(f"Error: {target} is not a directory")
        sys.exit(1)
    if not is_git_repo(target):
        print(f"Error: {target} is not a git repository")
        sys.exit(1)

    # Check for changes
    entries = get_status(target)
    if not entries:
        print("Nothing to commit — working tree clean.")
        sys.exit(0)

    print_change_summary(entries)

    # Get diff and generate message
    diff = get_diff(target)
    if not diff.strip():
        print("No diff content to send — nothing meaningful to commit.")
        sys.exit(0)

    if args.debug:
        print("─" * 60)
        print("DEBUG — diff sent to LLM:\n")
        print(diff)
        print("\n" + "─" * 60)

    print(f"Generating commit message with {config['model']}...")
    message = generate_message(diff, config)

    if test_mode:
        print("\n" + "─" * 60)
        print("TEST MODE — generated commit message:\n")
        print(message)
        print("\n" + "─" * 60)
        print("Ollama is reachable and responding. No changes were committed.")
        sys.exit(0)

    # Interaction loop
    while True:
        action, message = prompt_user(message)
        if action == "accept":
            do_commit_and_push(target, message)
            break
        if action == "regenerate":
            print(f"\nRegenerating with {config['model']}...")
            message = generate_message(diff, config)
            continue
        # quit
        print("Aborted.")
        break


if __name__ == "__main__":
    main()
