"""Tests for find_autostash_ref (branch-tagged autostash lookup)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_commit_core import find_autostash_ref


def test_finds_topmost_match():
    out = (
        "stash@{0}: On main: ai-commit-autostash:main\n"
        "stash@{1}: On feat: ai-commit-autostash:feat\n"
    )
    assert find_autostash_ref(out, "main") == "stash@{0}"
    assert find_autostash_ref(out, "feat") == "stash@{1}"


def test_returns_none_when_no_match():
    out = "stash@{0}: On main: WIP on main: 1234 msg\n"
    assert find_autostash_ref(out, "main") is None


def test_ignores_manual_stashes():
    out = (
        "stash@{0}: WIP on main: abc123 some manual stash\n"
        "stash@{1}: On main: ai-commit-autostash:main\n"
    )
    assert find_autostash_ref(out, "main") == "stash@{1}"


def test_picks_newest_for_duplicate_branch():
    out = (
        "stash@{0}: On main: ai-commit-autostash:main\n"
        "stash@{1}: On main: ai-commit-autostash:main\n"
    )
    assert find_autostash_ref(out, "main") == "stash@{0}"


def test_branch_prefix_not_confused():
    # 'feat' must not match a stash tagged for 'feature'
    out = "stash@{0}: On feature: ai-commit-autostash:feature\n"
    assert find_autostash_ref(out, "feat") is None


def test_detached_and_empty():
    assert find_autostash_ref("", "main") is None
    assert find_autostash_ref("stash@{0}: On x: ai-commit-autostash:HEAD\n", "HEAD") is None


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
