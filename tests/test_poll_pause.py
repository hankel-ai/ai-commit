"""When globally paused, bg_poll_repos must NOT rediscover/rescan other repos.

Regression for: while paused with one repo forced active, the poll still ran
`git rev-parse` (is_git_repo) on every repo each cycle. The paused fast-path
should poll only the active repo and reuse cache for the rest — no discovery.

Run: python tests/test_poll_pause.py
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


def _fake_repo(path):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=0, behind=0,
    )


def test_paused_polls_only_active_repo():
    calls = {"is_git_repo": 0, "status_paths": []}

    r1, r2, r3 = (f"C:/repos/r{i}" for i in (1, 2, 3))
    m.app = SimpleNamespace(
        repos={r1: _fake_repo(r1), r2: _fake_repo(r2), r3: _fake_repo(r3)},
        repo_overrides={r2: "active"},      # only r2 is forced active
        paused=True,
        watched_folders=["C:/repos"],       # must NOT be scanned while paused
        non_git_folders={},
    )

    posted = []
    m.ui_queue = SimpleNamespace(put=lambda item: posted.append(item))

    # Spies — discovery must not run; only the active repo gets a live status.
    # The poll path is folded onto read_status_branch (see
    # docs/polling-performance.md); spy that instead of the old get_status.
    def spy_is_git_repo(p):
        calls["is_git_repo"] += 1
        return True

    def spy_read_status_branch(p):
        calls["status_paths"].append(str(p))
        return True, [], {"branch": "main", "ahead": 0, "behind": 0,
                          "classification": "", "detached": False}

    m.is_git_repo = spy_is_git_repo
    m.read_status_branch = spy_read_status_branch
    m.fetch_remote = lambda p: None
    m.get_active_github_account = lambda: "tester"
    m.get_last_commit = lambda p: ("", "")
    m.get_remote_url = lambda p: ""
    m.get_github_account = lambda url: ""
    m.get_repo_visibility = lambda p: ""

    m.bg_poll_repos(force=False)

    check("no_discovery_rev_parse", calls["is_git_repo"] == 0)
    check("status_only_for_active", calls["status_paths"] == [str(Path(r2))])

    poll_results = [x for x in posted if x[0] == "poll_result"]
    check("one_poll_result_posted", len(poll_results) == 1)
    if poll_results:
        results = poll_results[0][1]
        check("all_three_repos_present", set(results.keys()) == {r1, r2, r3})


def main():
    test_paused_polls_only_active_repo()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
