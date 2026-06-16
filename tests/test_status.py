"""Tests for status error-vs-empty handling and per-repo git locking.

Regression coverage for the branch-switch race: a failed `git status` must not
be mistaken for a clean tree (which would bypass the switch confirm gate), and
git commands in the same repo must be serialized.

Run: python tests/test_status.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

# Several tests below monkeypatch core.run_git; keep the real one for the
# locking test, which must exercise the actual run_git implementation.
_ORIG_RUN_GIT = core.run_git

_failures = []


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


def test_read_status_error_is_not_clean():
    """rc!=0 must yield ok=False, never an empty (== clean) entry list."""
    core.run_git = lambda args, cwd: (1, "", "fatal: index.lock exists")
    ok, entries = core.read_status(".")
    check("error_marks_ok_false", ok is False)
    check("error_entries_empty", entries == [])


def test_read_status_clean_tree():
    core.run_git = lambda args, cwd: (0, "", "")
    ok, entries = core.read_status(".")
    check("clean_ok_true", ok is True)
    check("clean_entries_empty", entries == [])


def test_read_status_dirty_tree():
    porcelain = " M charts/a.yaml\n?? new/\nA  staged.txt\n"
    core.run_git = lambda args, cwd: (0, porcelain, "")
    ok, entries = core.read_status(".")
    check("dirty_ok_true", ok is True)
    check("dirty_count", len(entries) == 3)
    check("dirty_codes", [c for c, _ in entries] == ["M", "??", "A"])


def test_get_status_backcompat():
    """get_status still collapses error -> [] for existing callers."""
    core.run_git = lambda args, cwd: (1, "", "boom")
    check("get_status_error_empty", core.get_status(".") == [])
    core.run_git = lambda args, cwd: (0, " M x\n", "")
    check("get_status_dirty", len(core.get_status(".")) == 1)


def test_repo_lock_identity():
    a1 = core._repo_lock("C:/repo/Foo")
    a2 = core._repo_lock("C:/repo/Foo")
    b = core._repo_lock("C:/repo/Bar")
    check("same_path_same_lock", a1 is a2)
    check("diff_path_diff_lock", a1 is not b)


def test_run_git_serializes_per_repo():
    """Two threads running git in the same repo must not overlap."""
    # Restore a real-ish run_git that we can instrument for concurrency.
    overlap = {"max": 0, "cur": 0}
    guard = threading.Lock()

    real_subprocess_run = core.subprocess.run

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*a, **k):
        with guard:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        time.sleep(0.02)
        with guard:
            overlap["cur"] -= 1
        return _Result()

    core.run_git = _ORIG_RUN_GIT  # undo earlier tests' monkeypatch
    core.subprocess.run = fake_run
    core._GIT_LOGGER = None
    try:
        threads = [
            threading.Thread(target=core.run_git, args=(["status"], "C:/repo/Same"))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        core.subprocess.run = real_subprocess_run
    check("same_repo_never_overlaps", overlap["max"] == 1)


def main():
    test_read_status_error_is_not_clean()
    test_read_status_clean_tree()
    test_read_status_dirty_tree()
    test_get_status_backcompat()
    test_repo_lock_identity()
    test_run_git_serializes_per_repo()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
