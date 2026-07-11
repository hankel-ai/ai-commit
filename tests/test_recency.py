"""Tests for the recency tier predicates: ai_commit_core.is_repo_active and
ai_commit_core.is_folder_recent.

is_repo_active decides whether a repo is "active" (shown in the list and polled
every cycle) or "idle" (hidden by the recency filter and polled on the slow idle
cadence). A repo is active when the working tree is dirty, there are unpushed/
unpulled commits (ahead/behind), or the last commit is within recent_days.

is_folder_recent is the non-git-folder counterpart: recent when the folder's
mtime is within recent_days; an unknown mtime (0.0) counts as recent.

Run: python tests/test_recency.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

_failures = []

DAY = 86400
NOW = 1_700_000_000.0  # fixed reference "now" so tests are deterministic


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


# --- activity signals force "active" regardless of commit age ---------------

def test_dirty_forces_active():
    # Old commit (100 days ago) but working tree dirty -> active.
    old = NOW - 100 * DAY
    check("dirty_active", core.is_repo_active(old, True, 0, 0, NOW, 14) is True)


def test_ahead_forces_active():
    old = NOW - 100 * DAY
    check("ahead_active", core.is_repo_active(old, False, 2, 0, NOW, 14) is True)


def test_behind_forces_active():
    old = NOW - 100 * DAY
    check("behind_active", core.is_repo_active(old, False, 0, 3, NOW, 14) is True)


# --- recency window ----------------------------------------------------------

def test_recent_commit_is_active():
    ts = NOW - 5 * DAY  # 5 days ago, within 14
    check("recent_active", core.is_repo_active(ts, False, 0, 0, NOW, 14) is True)


def test_boundary_exactly_on_window_is_active():
    ts = NOW - 14 * DAY  # exactly at the edge -> inclusive
    check("boundary_active", core.is_repo_active(ts, False, 0, 0, NOW, 14) is True)


def test_just_beyond_window_is_idle():
    ts = NOW - 14 * DAY - 1  # one second past the window
    check("beyond_idle", core.is_repo_active(ts, False, 0, 0, NOW, 14) is False)


def test_old_clean_synced_is_idle():
    ts = NOW - 100 * DAY
    check("old_clean_idle", core.is_repo_active(ts, False, 0, 0, NOW, 14) is False)


def test_zero_ts_clean_is_idle():
    # No parseable commit date (e.g. unborn/failed) + clean + synced -> idle.
    check("zero_ts_idle", core.is_repo_active(0.0, False, 0, 0, NOW, 14) is False)


def test_zero_ts_but_dirty_is_active():
    check("zero_ts_dirty_active", core.is_repo_active(0.0, True, 0, 0, NOW, 14) is True)


def test_custom_window_widens():
    ts = NOW - 45 * DAY
    check("wide_window_idle_14", core.is_repo_active(ts, False, 0, 0, NOW, 14) is False)
    check("wide_window_active_90", core.is_repo_active(ts, False, 0, 0, NOW, 90) is True)


# --- non-git folder recency (is_folder_recent) -------------------------------

def test_folder_recent_mtime_shows():
    mt = NOW - 5 * DAY
    check("folder_recent", core.is_folder_recent(mt, NOW, 14) is True)


def test_folder_boundary_exactly_on_window_is_recent():
    mt = NOW - 14 * DAY  # exactly at the edge -> inclusive, same as repos
    check("folder_boundary_recent", core.is_folder_recent(mt, NOW, 14) is True)


def test_folder_just_beyond_window_is_old():
    mt = NOW - 14 * DAY - 1
    check("folder_beyond_old", core.is_folder_recent(mt, NOW, 14) is False)


def test_folder_zero_mtime_is_recent():
    # stat() failed / unknown mtime -> never silently hide the folder.
    check("folder_zero_mtime_recent", core.is_folder_recent(0.0, NOW, 14) is True)


def test_folder_custom_window_widens():
    mt = NOW - 45 * DAY
    check("folder_window_old_14", core.is_folder_recent(mt, NOW, 14) is False)
    check("folder_window_recent_90", core.is_folder_recent(mt, NOW, 90) is True)


def main():
    test_dirty_forces_active()
    test_ahead_forces_active()
    test_behind_forces_active()
    test_recent_commit_is_active()
    test_boundary_exactly_on_window_is_active()
    test_just_beyond_window_is_idle()
    test_old_clean_synced_is_idle()
    test_zero_ts_clean_is_idle()
    test_zero_ts_but_dirty_is_active()
    test_custom_window_widens()
    test_folder_recent_mtime_shows()
    test_folder_boundary_exactly_on_window_is_recent()
    test_folder_just_beyond_window_is_old()
    test_folder_zero_mtime_is_recent()
    test_folder_custom_window_widens()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
