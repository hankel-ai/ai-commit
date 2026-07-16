"""The no-poll re-render cache (app.last_results / app.last_non_git) must stay
in sync with what a single-repo rebuild actually renders.

Regression for: after a Commit & Push the repo's changes vanish (correct), but
ticking the "Date" sort checkbox brought them back. The sort/recency toggles
re-render from app.last_results, which is only refreshed by the full poll_result
handler. The post-commit single_repo_refresh (and refresh_then_generate) handler
rebuilt the visible UI from a fresh `merged` snapshot but never wrote it back to
app.last_results, so the toggle resurrected the stale pre-commit entries.

Run: python tests/test_last_results_cache.py
"""
import importlib.util
import os
import queue as _queue
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def _fake_repo(path, entries):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=entries, remote_url="",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", last_commit_ts=0.0,
        ahead=0, behind=0,
        gen_status=m.GenStatus.IDLE, error_message="", commit_message="",
    )


def _drive_single_refresh(stale_entries, fresh_info):
    """Simulate: repo r1 had `stale_entries` cached in last_results, then a
    commit refresh arrives with `fresh_info`. Returns app after the handler."""
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1, fresh_info["entries"])
    m.app = SimpleNamespace(
        repos={r1: rs}, non_git_folders={},
        last_results={r1: {"path": Path(r1), "entries": stale_entries,
                           "remote_url": "", "github_account": "", "visibility": "",
                           "branch": "main", "branch_status": "", "last_commit_msg": "",
                           "last_commit_date": "", "last_commit_ts": 0.0,
                           "ahead": 0, "behind": 0}},
        last_non_git={},
    )
    # Stub the actual dpg rebuild — we only care about the cache side effect.
    m.rebuild_repos_ui = lambda *a, **kw: None
    m._non_git_for_rebuild = lambda: {}

    q = _queue.Queue()
    q.put(("single_repo_refresh", r1, fresh_info, False))
    m.ui_queue = q
    m.process_queue()
    return r1


def test_last_results_synced_after_commit_refresh():
    """After the committed repo refreshes to a clean tree, the cached payload
    used by the Date/Recent toggles must show it clean too — not the old diff."""
    fresh_info = {"path": Path("C:/repos/r1"), "entries": [], "remote_url": "",
                  "github_account": "", "visibility": "", "branch": "main",
                  "branch_status": "", "last_commit_msg": "feat: x",
                  "last_commit_date": "2026-07-16", "last_commit_ts": 111.0,
                  "ahead": 0, "behind": 0}
    r1 = _drive_single_refresh(stale_entries=[("M", "a.py")], fresh_info=fresh_info)

    cached = m.app.last_results.get(r1, {})
    check("cache_present", bool(cached))
    check("cache_entries_fresh_not_stale", cached.get("entries") == [])
    check("cache_carries_new_commit_ts", cached.get("last_commit_ts") == 111.0)


def main():
    test_last_results_synced_after_commit_refresh()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
