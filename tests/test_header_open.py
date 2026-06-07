"""Tests for compute_header_open (repo header expand/collapse decision).

Covers the rule: repos must not be auto-collapsed on partial rebuilds
(single-repo refresh), only on full refreshes (poll loop / Refresh-all).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_commit_core import compute_header_open


def _call(**kw):
    base = dict(
        override="",
        paused=False,
        has_activity=False,
        force_expand=False,
        preserve_open=False,
        has_prior=False,
        prior_open=False,
    )
    base.update(kw)
    return compute_header_open(**base)


# --- Full refresh (preserve_open=False): activity-based default applies ---

def test_full_refresh_opens_active_repo():
    assert _call(has_activity=True) is True


def test_full_refresh_collapses_inactive_repo():
    assert _call(has_activity=False) is False


def test_full_refresh_ignores_prior_state():
    # On a full refresh prior open state is not consulted; activity wins.
    assert _call(has_activity=False, has_prior=True, prior_open=True) is False


# --- Partial refresh (preserve_open=True): keep the user's current state ---

def test_partial_keeps_user_expanded_inactive_repo():
    # The core bug fix: a repo the user expanded that has no activity must
    # NOT be auto-collapsed when another repo is refreshed.
    assert _call(preserve_open=True, has_prior=True, prior_open=True,
                 has_activity=False) is True


def test_partial_keeps_user_collapsed_active_repo():
    # Symmetric: a repo the user collapsed stays collapsed even if it now has
    # activity (we never auto-expand on a partial rebuild either).
    assert _call(preserve_open=True, has_prior=True, prior_open=False,
                 has_activity=True) is False


def test_partial_new_repo_falls_back_to_activity():
    # No prior state (e.g. a newly discovered repo) → activity default.
    assert _call(preserve_open=True, has_prior=False, has_activity=True) is True
    assert _call(preserve_open=True, has_prior=False, has_activity=False) is False


# --- Force-pause override always wins ---

def test_force_pause_collapses_even_if_prior_open():
    assert _call(override="pause", preserve_open=True, has_prior=True,
                 prior_open=True, has_activity=True) is False


# --- Global pause collapses on full refresh ---

def test_global_pause_collapses():
    assert _call(paused=True, has_activity=True) is False


def test_global_pause_force_active_uses_activity():
    assert _call(paused=True, override="active", has_activity=True) is True
    assert _call(paused=True, override="active", has_activity=False) is False


def test_global_pause_force_active_partial_keeps_prior():
    # Force-active repo on a partial rebuild keeps its prior open state.
    assert _call(paused=True, override="active", preserve_open=True,
                 has_prior=True, prior_open=True, has_activity=False) is True


# --- force_expand (single-repo refresh of a paused repo) ---

def test_force_expand_overrides_pause_and_uses_activity():
    # force_expand skips both the pause-collapse and the preserve path,
    # showing the repo's natural activity-based state.
    assert _call(paused=True, force_expand=True, preserve_open=True,
                 has_prior=True, prior_open=False, has_activity=True) is True
    assert _call(paused=True, force_expand=True, has_activity=False) is False


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
