"""Tests for the folded poll path: parse_branch_header + read_status_branch.

Option 1 of the poll-performance work (see docs/polling-performance.md) folds
4 separate git calls (status, current-branch, ahead/behind, branch
classification) into a single `git status --porcelain --branch`. The header
parser must handle every shape git emits, and read_status_branch must keep the
`ok=False` failure contract that read_status guarantees.

Run: python tests/test_status_branch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

_failures = []


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


# --- parse_branch_header: every header shape git can emit -------------------

def test_synced():
    h = core.parse_branch_header("## main...origin/main")
    check("synced_branch", h["branch"] == "main")
    check("synced_ahead", h["ahead"] == 0)
    check("synced_behind", h["behind"] == 0)
    check("synced_class", h["classification"] == "")
    check("synced_detached", h["detached"] is False)


def test_ahead_and_behind():
    h = core.parse_branch_header("## main...origin/main [ahead 1, behind 2]")
    check("ab_ahead", h["ahead"] == 1)
    check("ab_behind", h["behind"] == 2)
    check("ab_class", h["classification"] == "")


def test_ahead_only():
    h = core.parse_branch_header("## main...origin/main [ahead 3]")
    check("ahead_only_ahead", h["ahead"] == 3)
    check("ahead_only_behind", h["behind"] == 0)


def test_behind_only():
    h = core.parse_branch_header("## main...origin/main [behind 4]")
    check("behind_only_ahead", h["ahead"] == 0)
    check("behind_only_behind", h["behind"] == 4)


def test_gone_is_stale():
    h = core.parse_branch_header("## main...origin/main [gone]")
    check("gone_class", h["classification"] == "stale")
    check("gone_branch", h["branch"] == "main")
    check("gone_ahead", h["ahead"] == 0)


def test_local_only():
    h = core.parse_branch_header("## feature")
    check("local_branch", h["branch"] == "feature")
    check("local_class", h["classification"] == "local only")


def test_detached():
    h = core.parse_branch_header("## HEAD (no branch)")
    check("detached_branch", h["branch"] == "HEAD")
    check("detached_flag", h["detached"] is True)
    check("detached_class", h["classification"] == "")


def test_unborn():
    h = core.parse_branch_header("## No commits yet on main")
    check("unborn_branch", h["branch"] == "main")
    check("unborn_class", h["classification"] == "")


def test_branch_with_slash():
    h = core.parse_branch_header("## feat/foo...origin/feat/foo [ahead 1]")
    check("slash_branch", h["branch"] == "feat/foo")
    check("slash_ahead", h["ahead"] == 1)


def test_garbage_line():
    h = core.parse_branch_header("not a header")
    check("garbage_branch", h["branch"] == "")
    check("garbage_class", h["classification"] == "")


# --- read_status_branch: entries + ok contract ------------------------------

def test_read_status_branch_failure_is_not_clean():
    """rc!=0 -> ok False, empty entries (same contract as read_status)."""
    core.run_git = lambda args, cwd: (1, "", "fatal: index.lock exists")
    ok, entries, info = core.read_status_branch(".")
    check("rsb_error_ok_false", ok is False)
    check("rsb_error_entries", entries == [])
    check("rsb_error_branch", info["branch"] == "")


def test_read_status_branch_parses_entries_and_header():
    out = ("## main...origin/main [ahead 2]\n"
           " M charts/a.yaml\n"
           "?? new/\n"
           "A  staged.txt\n")
    core.run_git = lambda args, cwd: (0, out, "")
    ok, entries, info = core.read_status_branch(".")
    check("rsb_ok_true", ok is True)
    check("rsb_branch", info["branch"] == "main")
    check("rsb_ahead", info["ahead"] == 2)
    check("rsb_entry_count", len(entries) == 3)
    check("rsb_entry_codes", [c for c, _ in entries] == ["M", "??", "A"])
    check("rsb_header_excluded", all(p != "main...origin/main" for _, p in entries))


def test_read_status_branch_clean_tree():
    core.run_git = lambda args, cwd: (0, "## main...origin/main\n", "")
    ok, entries, info = core.read_status_branch(".")
    check("rsb_clean_ok", ok is True)
    check("rsb_clean_entries", entries == [])
    check("rsb_clean_branch", info["branch"] == "main")


def main():
    test_synced()
    test_ahead_and_behind()
    test_ahead_only()
    test_behind_only()
    test_gone_is_stale()
    test_local_only()
    test_detached()
    test_unborn()
    test_branch_with_slash()
    test_garbage_line()
    test_read_status_branch_failure_is_not_clean()
    test_read_status_branch_parses_entries_and_header()
    test_read_status_branch_clean_tree()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
