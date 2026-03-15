#!/usr/bin/env python3
"""AI Commit Message Generator — CLI wrapper using ai_commit_core."""

import argparse
import os
import sys
from pathlib import Path

from ai_commit_core import (
    STATUS_LABELS,
    OllamaError,
    do_commit_and_push,
    generate_message,
    get_diff,
    get_status,
    is_git_repo,
)


# ---------------------------------------------------------------------------
# CLI-only helpers
# ---------------------------------------------------------------------------

def print_change_summary(entries):
    """Print a human-readable summary of changed files."""
    print("\nChanged files:")
    for code, filepath in entries:
        label = STATUS_LABELS.get(code, code)
        print(f"  {label:>10}  {filepath}")
    print()


def prompt_user(message):
    """Show the commit message and let the user accept, regenerate, edit, or quit.

    Returns (action, message) where action is 'accept', 'regenerate', or 'quit'.
    """
    print("\u2500" * 60)
    print("Proposed commit message:\n")
    print(message)
    print("\n" + "\u2500" * 60)
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
            print("Empty message \u2014 aborting.")
            return "quit", message
        return "accept", new_msg
    return "quit", message


# ---------------------------------------------------------------------------
# Args & main
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
        print("Nothing to commit \u2014 working tree clean.")
        sys.exit(0)

    print_change_summary(entries)

    # Get diff and generate message
    diff = get_diff(target)
    if not diff.strip():
        print("No diff content to send \u2014 nothing meaningful to commit.")
        sys.exit(0)

    if args.debug:
        print("\u2500" * 60)
        print("DEBUG \u2014 diff sent to LLM:\n")
        print(diff)
        print("\n" + "\u2500" * 60)

    print(f"Generating commit message with {config['model']}...")
    try:
        message = generate_message(diff, config)
    except OllamaError as exc:
        print(f"\nError: {exc}")
        sys.exit(1)

    if test_mode:
        print("\n" + "\u2500" * 60)
        print("TEST MODE \u2014 generated commit message:\n")
        print(message)
        print("\n" + "\u2500" * 60)
        print("Ollama is reachable and responding. No changes were committed.")
        sys.exit(0)

    # Interaction loop
    while True:
        action, message = prompt_user(message)
        if action == "accept":
            ok, detail = do_commit_and_push(target, message)
            print(detail)
            if not ok:
                sys.exit(1)
            break
        if action == "regenerate":
            print(f"\nRegenerating with {config['model']}...")
            try:
                message = generate_message(diff, config)
            except OllamaError as exc:
                print(f"\nError: {exc}")
                sys.exit(1)
            continue
        # quit
        print("Aborted.")
        break


if __name__ == "__main__":
    main()
