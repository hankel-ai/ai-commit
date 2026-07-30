"""Tests for git's "dubious ownership" refusal detection and the post-init
repo verification that surfaces it.

Background: `git init` succeeds in a directory whose *owner* is not the current
user (e.g. one created by an elevated shell -- Windows stamps the owner as
BUILTIN\\Administrators). Every git command afterwards fails with rc=128, so
is_git_repo() reports False and the GUI re-renders the folder with an Init
button -- an invisible loop where Init "works" forever and nothing changes.

Headless: imports only ai_commit_core (no Dear PyGui).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core


DUBIOUS_STDERR = (
    "fatal: detected dubious ownership in repository at "
    "'C:/Users/admin/OneDrive/ClaudeCode/cc-tmux'\n"
    "'C:/Users/admin/OneDrive/ClaudeCode/cc-tmux' is owned by:\n"
    "\t'S-1-5-32-544'\n"
    "but the current user is:\n"
    "\t'S-1-5-21-1915601546-625631575-1468348727-1001'\n"
    "To add an exception for this directory, call:\n\n"
    "\tgit config --global --add safe.directory "
    "C:/Users/admin/OneDrive/ClaudeCode/cc-tmux\n"
)


# ---------------------------------------------------------------------------
# is_dubious_ownership
# ---------------------------------------------------------------------------

def test_detects_dubious_ownership():
    assert ai_commit_core.is_dubious_ownership(DUBIOUS_STDERR)


def test_detects_unsafe_repository_wording():
    # Older git phrasing for the same safe.directory refusal.
    assert ai_commit_core.is_dubious_ownership(
        "fatal: unsafe repository ('/repo' is owned by someone else)"
    )


def test_ignores_unrelated_errors():
    assert not ai_commit_core.is_dubious_ownership("error: No such remote 'origin'")
    assert not ai_commit_core.is_dubious_ownership("")
    assert not ai_commit_core.is_dubious_ownership(None)


# ---------------------------------------------------------------------------
# describe_dubious_ownership
# ---------------------------------------------------------------------------

def test_describe_names_owner_and_both_fixes():
    msg = ai_commit_core.describe_dubious_ownership(
        r"C:\Users\admin\OneDrive\ClaudeCode\cc-tmux", DUBIOUS_STDERR)
    # Says what is wrong, in the app's own words rather than raw git output.
    assert "owner" in msg.lower()
    # The owning SID git reported is carried through -- S-1-5-32-544 is the
    # giveaway that an elevated process created the folder.
    assert "S-1-5-32-544" in msg
    # Both remedies are offered: fix the owner (preferred) or trust the path.
    assert "icacls" in msg
    assert "safe.directory" in msg
    assert "cc-tmux" in msg


def test_describe_without_stderr_still_actionable():
    msg = ai_commit_core.describe_dubious_ownership(r"C:\proj\foo", "")
    assert "icacls" in msg
    assert "safe.directory" in msg


# ---------------------------------------------------------------------------
# verify_repo_usable -- the guard that makes the silent Init loop impossible
# ---------------------------------------------------------------------------

def test_verify_repo_usable_on_fresh_init():
    tmp = tempfile.mkdtemp()
    try:
        rc, _, _ = ai_commit_core.run_git(["init", "-b", "main"], cwd=tmp)
        assert rc == 0
        ok, detail = ai_commit_core.verify_repo_usable(tmp)
        assert ok, detail
        assert detail == ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_repo_usable_fails_on_non_repo():
    tmp = tempfile.mkdtemp()
    try:
        ok, detail = ai_commit_core.verify_repo_usable(tmp)
        assert not ok
        assert detail  # must explain itself, never fail silently
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_repo_usable_explains_dubious_ownership(monkeypatch=None):
    """A repo git refuses to touch reports the ownership explanation, not
    git's raw multi-line fatal."""
    calls = []

    def fake_run_git(args, cwd):
        calls.append(args)
        return 128, "", DUBIOUS_STDERR

    real = ai_commit_core.run_git
    ai_commit_core.run_git = fake_run_git
    try:
        ok, detail = ai_commit_core.verify_repo_usable(r"C:\repo\cc-tmux")
        assert not ok
        assert "S-1-5-32-544" in detail
        assert "icacls" in detail
        # Not just the raw git text dumped through.
        assert "detected dubious ownership" not in detail
    finally:
        ai_commit_core.run_git = real
    assert calls, "verify_repo_usable must actually ask git"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
