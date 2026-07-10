"""A manual per-repo Refresh (force=True) must clear that repo's sticky error.

Regression for: after a failed push the repo shows a sticky GenStatus.ERROR.
While Paused, only the full Refresh-all cleared it (poll_result plumbs
force -> clear_errors); the per-repo context-menu Refresh went through
bg_refresh_single_repo, whose queue message dropped the force flag, so the
sticky-error branch in rebuild_repos_ui kept the error alive forever.

Run: python tests/test_single_refresh_error.py
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Neutralize the GUI's startup self-detach so exec_module just defines symbols.
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _fake_repo(path, gen_status, error_message=""):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=gen_status, error_message=error_message, commit_message="",
    )


def _stub_git_ops():
    m.get_last_commit = lambda p: ("", "")
    m.get_remote_url = lambda p: ""
    m.get_github_account = lambda url: ""
    m.get_repo_visibility = lambda p: ""
    m.fetch_remote = lambda p: None
    m.read_status_branch = lambda p: (
        True, [], {"branch": "main", "ahead": 1, "behind": 0,
                   "classification": "", "detached": False})


def test_bg_refresh_single_repo_posts_force_flag():
    """The queue message must carry the force flag to the handler."""
    r1 = "C:/repos/r1"
    m.app = SimpleNamespace(
        repos={r1: _fake_repo(r1, m.GenStatus.ERROR, "Push failed: blocked")},
        non_git_folders={},
    )
    posted = []
    m.ui_queue = SimpleNamespace(put=lambda item: posted.append(item))
    _stub_git_ops()

    m.bg_refresh_single_repo(r1, force=True)

    refresh_msgs = [x for x in posted if x[0] == "single_repo_refresh"]
    check("one_refresh_posted", len(refresh_msgs) == 1)
    check("force_flag_in_message",
          len(refresh_msgs[0]) > 3 and refresh_msgs[0][3] is True)


def _run_handler(force):
    """Drive process_queue with a single_repo_refresh message; return the repo."""
    import queue as _queue
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1, m.GenStatus.ERROR, "Push failed: blocked")
    m.app = SimpleNamespace(repos={r1: rs}, non_git_folders={})

    rebuilds = []
    m.rebuild_repos_ui = lambda *a, **kw: rebuilds.append((a, kw))

    info = {
        "path": Path(r1), "entries": [], "remote_url": "",
        "github_account": "", "visibility": "", "branch": "main",
        "branch_status": "", "last_commit_msg": "", "last_commit_date": "",
        "ahead": 1, "behind": 0,
    }
    q = _queue.Queue()
    q.put(("single_repo_refresh", r1, info, force))
    m.ui_queue = q
    m.process_queue()
    return rs, rebuilds


def test_forced_single_refresh_clears_sticky_error():
    rs, rebuilds = _run_handler(force=True)
    check("error_message_cleared", rs.error_message == "")
    check("gen_status_reset", rs.gen_status == m.GenStatus.IDLE)
    check("rebuild_ran", len(rebuilds) == 1)
    if rebuilds:
        check("rebuild_preserves_open",
              rebuilds[0][1].get("preserve_open") is True)


def test_unforced_single_refresh_keeps_sticky_error():
    """Automatic refreshes (post-commit, branch ops) must NOT clear errors."""
    rs, rebuilds = _run_handler(force=False)
    check("error_message_kept", rs.error_message == "Push failed: blocked")
    check("gen_status_kept", rs.gen_status == m.GenStatus.ERROR)
    check("rebuild_still_ran", len(rebuilds) == 1)


def main():
    test_bg_refresh_single_repo_posts_force_flag()
    test_forced_single_refresh_clears_sticky_error()
    test_unforced_single_refresh_keeps_sticky_error()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
