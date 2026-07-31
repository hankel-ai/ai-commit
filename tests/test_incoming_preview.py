"""Tests for the Preview Pull structured payload.

Preview Pull used to hand the UI two opaque strings (`git log --oneline` and
`git diff --stat`). To make each commit and each file individually clickable it
now returns parsed records, so the parsers are what this file covers.

The `-z` numstat form is the reason these tests exist: an unquoted `--numstat`
renders a rename as `a/{b => c}/d`, which is NOT a valid pathspec -- feeding it
back to `git diff -- <path>` for the row's "View Diff" button fails. With -z a
rename instead emits an empty path field followed by two more NUL fields.

Run: python tests/test_incoming_preview.py
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


# --- parse_incoming_log -----------------------------------------------------

def test_log_basic():
    out = "a1b2c3d\0fix(helm): bump chart version\ne4f5g6h\0feat: add probes\n"
    commits = core.parse_incoming_log(out)
    check("log_count", len(commits) == 2)
    check("log_sha", commits[0]["sha"] == "a1b2c3d")
    check("log_subject", commits[0]["subject"] == "fix(helm): bump chart version")
    check("log_order_newest_first", commits[1]["sha"] == "e4f5g6h")


def test_log_subject_with_odd_whitespace():
    # A subject containing a tab or leading spaces must survive intact -- this
    # is why the format is NUL-delimited rather than --oneline.
    out = "abc1234\0fix:\tuse  spaced   words\n"
    commits = core.parse_incoming_log(out)
    check("log_tab_subject", commits[0]["subject"] == "fix:\tuse  spaced   words")


def test_log_empty_subject():
    commits = core.parse_incoming_log("abc1234\0\n")
    check("log_empty_subject_kept", len(commits) == 1)
    check("log_empty_subject_value", commits[0]["subject"] == "")


def test_log_empty_and_blank():
    check("log_empty", core.parse_incoming_log("") == [])
    check("log_whitespace", core.parse_incoming_log("\n\n") == [])


def test_log_no_trailing_newline():
    commits = core.parse_incoming_log("abc1234\0only commit")
    check("log_no_trailing_nl", len(commits) == 1 and commits[0]["sha"] == "abc1234")


# --- parse_numstat_z --------------------------------------------------------

def test_numstat_basic():
    out = "2\t1\thelm/dxs/Chart.yaml\0" "12\t2\thelm/dxs/values.yaml\0"
    files = core.parse_numstat_z(out)
    check("numstat_count", len(files) == 2)
    check("numstat_path", files[0]["path"] == "helm/dxs/Chart.yaml")
    check("numstat_added", files[0]["added"] == 2)
    check("numstat_deleted", files[0]["deleted"] == 1)
    check("numstat_not_binary", files[0]["binary"] is False)
    check("numstat_second", files[1]["added"] == 12 and files[1]["deleted"] == 2)


def test_numstat_binary():
    files = core.parse_numstat_z("-\t-\tassets/logo.png\0")
    check("binary_flag", files[0]["binary"] is True)
    check("binary_path", files[0]["path"] == "assets/logo.png")
    check("binary_counts_zero",
          files[0]["added"] == 0 and files[0]["deleted"] == 0)


def test_numstat_rename():
    # Rename: counts, empty path field, then old and new paths.
    out = "0\t0\t\0old/name.py\0new/name.py\0"
    files = core.parse_numstat_z(out)
    check("rename_count", len(files) == 1)
    check("rename_new_path", files[0]["path"] == "new/name.py")
    check("rename_old_path", files[0]["old_path"] == "old/name.py")


def test_numstat_rename_with_edits_then_more_files():
    # A rename record must consume exactly 3 fields so the following ordinary
    # record is not misread as its path.
    out = ("5\t3\t\0src/a.py\0src/b.py\0"
           "1\t0\tREADME.md\0")
    files = core.parse_numstat_z(out)
    check("rename_then_count", len(files) == 2)
    check("rename_then_first", files[0]["path"] == "src/b.py")
    check("rename_then_edits", files[0]["added"] == 5 and files[0]["deleted"] == 3)
    check("rename_then_second", files[1]["path"] == "README.md")
    check("rename_then_second_no_old", files[1]["old_path"] == "")


def test_numstat_path_with_spaces():
    files = core.parse_numstat_z("1\t1\tdocs/my notes file.md\0")
    check("space_path", files[0]["path"] == "docs/my notes file.md")


def test_numstat_empty():
    check("numstat_empty", core.parse_numstat_z("") == [])
    check("numstat_only_nul", core.parse_numstat_z("\0") == [])


def test_numstat_truncated_rename():
    # Malformed tail (rename header with no following paths) must not raise.
    files = core.parse_numstat_z("0\t0\t\0old/only.py\0")
    check("truncated_rename_no_crash", files == [])


# --- get_incoming_changes (stubbed run_git) ---------------------------------

class _GitStub:
    """Stands in for core.run_git, answering by git subcommand."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, cwd):
        self.calls.append(args)
        for key, value in self.responses.items():
            if key in args:
                return value
        return 1, "", "unexpected call"


def _with_stub(stub, fn):
    original = core.run_git
    core.run_git = stub
    try:
        return fn()
    finally:
        core.run_git = original


def test_incoming_no_upstream():
    stub = _GitStub({"rev-parse": (128, "", "no upstream configured")})
    result = _with_stub(stub, lambda: core.get_incoming_changes("/repo"))
    check("no_upstream_result", result == ("", [], []))
    check("no_upstream_short_circuits", len(stub.calls) == 1)


def test_incoming_empty_upstream_string():
    stub = _GitStub({"rev-parse": (0, "\n", "")})
    result = _with_stub(stub, lambda: core.get_incoming_changes("/repo"))
    check("blank_upstream_result", result == ("", [], []))


def test_incoming_happy_path():
    stub = _GitStub({
        "rev-parse": (0, "origin/main\n", ""),
        "log": (0, "a1b2c3d\0fix: one\ne4f5g6h\0feat: two\n", ""),
        "diff": (0, "2\t1\tChart.yaml\0-\t-\tlogo.png\0", ""),
    })
    upstream, commits, files = _with_stub(
        stub, lambda: core.get_incoming_changes("/repo"))
    check("happy_upstream", upstream == "origin/main")
    check("happy_commits", len(commits) == 2)
    check("happy_commit_subject", commits[0]["subject"] == "fix: one")
    check("happy_files", len(files) == 2)
    check("happy_binary", files[1]["binary"] is True)


def test_incoming_log_failure_keeps_files():
    # A failing `git log` must not lose the file list (or raise).
    stub = _GitStub({
        "rev-parse": (0, "origin/main\n", ""),
        "log": (1, "", "fatal: bad revision"),
        "diff": (0, "2\t1\tChart.yaml\0", ""),
    })
    upstream, commits, files = _with_stub(
        stub, lambda: core.get_incoming_changes("/repo"))
    check("log_fail_upstream", upstream == "origin/main")
    check("log_fail_commits_empty", commits == [])
    check("log_fail_files_kept", len(files) == 1)


def test_incoming_uses_three_dot_range_for_diff():
    # HEAD...upstream (merge-base) is what a pull would actually apply;
    # HEAD..upstream would also show local-only commits inverted.
    stub = _GitStub({
        "rev-parse": (0, "origin/main\n", ""),
        "log": (0, "", ""),
        "diff": (0, "", ""),
    })
    _with_stub(stub, lambda: core.get_incoming_changes("/repo"))
    diff_call = [c for c in stub.calls if "diff" in c][0]
    check("diff_three_dot", "HEAD...origin/main" in diff_call)
    check("diff_is_z", "-z" in diff_call and "--numstat" in diff_call)
    log_call = [c for c in stub.calls if "log" in c][0]
    check("log_two_dot", "HEAD..origin/main" in log_call)


# --- get_commit_patch -------------------------------------------------------

def test_commit_patch_normal():
    patch = "commit a1b2c3d\n\ndiff --git a/x b/x\n+added\n"
    stub = _GitStub({"show": (0, patch, "")})
    ok, text = _with_stub(stub, lambda: core.get_commit_patch("/repo", "a1b2c3d"))
    check("patch_ok", ok is True)
    check("patch_text", text == patch)
    check("patch_first_parent", "--first-parent" in stub.calls[0])
    check("patch_no_fallback", len(stub.calls) == 1)


def test_commit_patch_merge_falls_back():
    # `git show` on a merge can print the message with no patch body; the
    # explicit range fills it in.
    stub = _GitStub({
        "show": (0, "commit deadbee\nMerge: 111 222\n\n    Merge branch 'x'\n", ""),
        "diff": (0, "diff --git a/y b/y\n+merged\n", ""),
    })
    ok, text = _with_stub(stub, lambda: core.get_commit_patch("/repo", "deadbee"))
    check("merge_ok", ok is True)
    check("merge_has_message", "Merge branch 'x'" in text)
    check("merge_has_patch", "diff --git a/y b/y" in text)
    check("merge_used_range", "deadbee^1..deadbee" in stub.calls[1])


def test_commit_patch_failure():
    stub = _GitStub({"show": (128, "", "fatal: bad object nope\n")})
    ok, text = _with_stub(stub, lambda: core.get_commit_patch("/repo", "nope"))
    check("patch_fail_ok", ok is False)
    check("patch_fail_text", "bad object" in text)


def _run_all():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
