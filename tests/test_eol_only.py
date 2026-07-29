"""Tests for EOL-only ("nothing to commit") change detection.

Background: with git's check-in normalization on (``core.autocrlf=input`` or
``true``, or a ``* text=auto`` .gitattributes rule), a file whose *only* change
is CRLF<->LF still shows up in ``git status --porcelain`` as ` M`, but every
content diff (``git diff HEAD``, ``--stat``, ``--name-only``) is EMPTY -- the
working copy and the committed blob are identical once normalized. ``git
commit`` in that state says "nothing to commit".

ai-commit used to report a bare "No diff content available." for this, which
looked like a bug in the tool. These tests pin the explanatory behaviour.

Headless: imports only ai_commit_core (no Dear PyGui).
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_is_eol_only_change_crlf_vs_lf():
    assert ai_commit_core.is_eol_only_change(b"a\nb\n", b"a\r\nb\r\n")
    assert ai_commit_core.is_eol_only_change(b"a\r\nb\r\n", b"a\nb\n")


def test_is_eol_only_change_bare_cr():
    assert ai_commit_core.is_eol_only_change(b"a\nb\n", b"a\rb\r")


def test_is_eol_only_change_partial_conversion():
    # Only some lines converted -- still purely an EOL change.
    assert ai_commit_core.is_eol_only_change(b"a\nb\nc\n", b"a\r\nb\nc\r\n")


def test_is_eol_only_change_false_when_identical():
    # Nothing changed at all -- there is no EOL story to tell.
    assert not ai_commit_core.is_eol_only_change(b"a\nb\n", b"a\nb\n")


def test_is_eol_only_change_false_for_real_edit():
    assert not ai_commit_core.is_eol_only_change(b"a\nb\n", b"a\r\nb\r\nc\r\n")
    assert not ai_commit_core.is_eol_only_change(b"a\nb\n", b"a\nB\n")


def test_is_eol_only_change_false_for_binary_ish():
    # A lone \r inside binary data must not make two different blobs "EOL-only".
    assert not ai_commit_core.is_eol_only_change(b"\x00\r\x01", b"\x00\n\x02")


def test_describe_eol_style():
    assert ai_commit_core.describe_eol_style(b"a\r\nb\r\n") == "CRLF"
    assert ai_commit_core.describe_eol_style(b"a\nb\n") == "LF"
    assert ai_commit_core.describe_eol_style(b"a\rb\r") == "CR"
    assert ai_commit_core.describe_eol_style(b"a\r\nb\n") == "mixed CRLF+LF"
    assert ai_commit_core.describe_eol_style(b"abc") == "no line endings"


# ---------------------------------------------------------------------------
# describe_empty_diff against real throwaway repos
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t"] + list(args),
        cwd=repo, check=True, capture_output=True,
    )


def _make_repo(autocrlf, committed_bytes, worktree_bytes, name="f.sh"):
    """Repo with *name* committed, then rewritten in the worktree."""
    repo = tempfile.mkdtemp(prefix="acg_eol_test_")
    subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
    _git(repo, "config", "core.autocrlf", autocrlf)
    with open(os.path.join(repo, name), "wb") as fh:
        fh.write(committed_bytes)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", "init")
    with open(os.path.join(repo, name), "wb") as fh:
        fh.write(worktree_bytes)
    return repo


def test_lf_to_crlf_under_autocrlf_input():
    # The reported case: git status says "modified", git diff says nothing.
    repo = _make_repo("input", b"alpha\nbeta\n", b"alpha\r\nbeta\r\n")
    try:
        assert ai_commit_core.get_status(repo), "expected a dirty status entry"
        assert ai_commit_core.get_diff(repo).strip() == "", "expected an empty diff"

        detail = ai_commit_core.describe_empty_diff(repo)
        assert "f.sh" in detail
        assert "line ending" in detail.lower()
        assert "CRLF" in detail and "LF" in detail
        assert "core.autocrlf=input" in detail
        assert "renormalize" in detail
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_crlf_to_lf_under_autocrlf_true():
    # autocrlf=true (typical Windows work machine): blob is LF, checkout is
    # CRLF; converting the worktree file to LF is also a no-op for git.
    repo = _make_repo("true", b"alpha\r\nbeta\r\n", b"alpha\nbeta\n")
    try:
        assert ai_commit_core.get_diff(repo).strip() == ""
        detail = ai_commit_core.describe_empty_diff(repo)
        assert "f.sh" in detail
        assert "line ending" in detail.lower()
        assert "core.autocrlf=true" in detail
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_single_file_scope():
    repo = _make_repo("input", b"alpha\nbeta\n", b"alpha\r\nbeta\r\n")
    try:
        with open(os.path.join(repo, "other.txt"), "wb") as fh:
            fh.write(b"untouched\n")
        assert "f.sh" in ai_commit_core.describe_empty_diff(repo, only_path="f.sh")
        assert ai_commit_core.describe_empty_diff(repo, only_path="other.txt") == ""
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_silent_on_clean_repo():
    repo = _make_repo("input", b"alpha\n", b"alpha\n")
    try:
        assert ai_commit_core.describe_empty_diff(repo) == ""
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_silent_on_real_change():
    # A genuine edit produces a real diff -- never claim it is EOL-only.
    repo = _make_repo("input", b"alpha\n", b"alpha\ngamma\n")
    try:
        assert ai_commit_core.get_diff(repo).strip() != ""
        assert ai_commit_core.describe_empty_diff(repo) == ""
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_silent_on_untracked_only():
    # Untracked files already get their content into the diff; nothing to explain.
    repo = tempfile.mkdtemp(prefix="acg_eol_test_")
    try:
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        with open(os.path.join(repo, "new.txt"), "wb") as fh:
            fh.write(b"hello\n")
        assert ai_commit_core.describe_empty_diff(repo) == ""
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_run_git_bytes_preserves_crlf():
    # run_git() is text-mode: universal newlines silently turn \r\n into \n,
    # which would make every file look like it had LF endings.
    repo = _make_repo("false", b"alpha\r\nbeta\r\n", b"alpha\r\nbeta\r\n")
    try:
        rc, out, _ = ai_commit_core.run_git_bytes(["show", "HEAD:f.sh"], cwd=repo)
        assert rc == 0
        assert out == b"alpha\r\nbeta\r\n"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
