"""The PUBLIC/PRIVATE badge must not cost a `gh repo view` on every launch.

get_repo_visibility shells out to `gh repo view` (~440 ms of network) and was
cached only on RepoState, which starts empty. At startup every repo is "new",
so a 51-remote launch spent ~22 s re-deriving a badge that essentially never
changes -- and that time is invisible in the activity log, because gh runs via
subprocess.run rather than run_git.

Run: python tests/test_visibility_cache.py
"""
import importlib.util
import json
import os
import sys
import tempfile
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

R1 = "C:/repos/r1"
REMOTE = "https://github.com/hankel-ai/hermes.git"


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _fake_repo(path, visibility=""):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url=REMOTE,
        github_account="", visibility=visibility, branch="main",
        branch_status="", last_commit_msg="", last_commit_date="",
        last_commit_ts=0.0, ahead=0, behind=0,
    )


def _spy_gh(answer="PRIVATE"):
    calls = []
    m.get_repo_visibility = lambda p: (calls.append(str(p)) or answer)
    return calls


def test_cache_hit_skips_gh():
    m.app = m.AppState(visibility_cache={R1: "PRIVATE"})
    calls = _spy_gh()
    vis = m._repo_visibility_cached(Path(R1), R1, None, False, REMOTE)
    check("cache_hit_value", vis == "PRIVATE")
    check("cache_hit_no_gh_call", calls == [])
    check("cache_hit_not_dirty", m.app.visibility_cache_dirty is False)


def test_cached_empty_also_counts_as_answered():
    """A non-GitHub remote (GitLab) answers "" -- that must stick, or those
    repos re-pay the gh cost every single launch."""
    m.app = m.AppState(visibility_cache={R1: ""})
    calls = _spy_gh()
    vis = m._repo_visibility_cached(Path(R1), R1, None, False, REMOTE)
    check("empty_is_a_hit", vis == "" and calls == [])


def test_miss_calls_gh_and_populates_cache():
    m.app = m.AppState()
    calls = _spy_gh("PUBLIC")
    vis = m._repo_visibility_cached(Path(R1), R1, None, False, REMOTE)
    check("miss_returns_gh_answer", vis == "PUBLIC")
    check("miss_called_gh_once", len(calls) == 1)
    check("miss_populated_cache", m.app.visibility_cache == {R1: "PUBLIC"})
    check("miss_marked_dirty", m.app.visibility_cache_dirty is True)


def test_session_state_wins_over_cache():
    m.app = m.AppState(visibility_cache={R1: "PUBLIC"})
    calls = _spy_gh()
    vis = m._repo_visibility_cached(Path(R1), R1, _fake_repo(R1, "PRIVATE"),
                                    False, REMOTE)
    check("session_value_preferred", vis == "PRIVATE" and calls == [])


def test_force_reasks_and_overwrites():
    """Manual Refresh / force-active is the escape hatch when a repo flips
    visibility or gains a GitHub remote."""
    m.app = m.AppState(visibility_cache={R1: "PRIVATE"})
    calls = _spy_gh("PUBLIC")
    vis = m._repo_visibility_cached(Path(R1), R1, _fake_repo(R1, "PRIVATE"),
                                    True, REMOTE)
    check("force_calls_gh", len(calls) == 1)
    check("force_returns_fresh", vis == "PUBLIC")
    check("force_overwrites_cache", m.app.visibility_cache[R1] == "PUBLIC")
    check("force_marked_dirty", m.app.visibility_cache_dirty is True)


def test_unchanged_answer_does_not_dirty_settings():
    """A forced re-ask that confirms the cached value shouldn't rewrite the
    settings file."""
    m.app = m.AppState(visibility_cache={R1: "PUBLIC"})
    _spy_gh("PUBLIC")
    m._repo_visibility_cached(Path(R1), R1, None, True, REMOTE)
    check("no_change_no_dirty", m.app.visibility_cache_dirty is False)


def test_no_remote_never_calls_gh():
    m.app = m.AppState()
    calls = _spy_gh()
    vis = m._repo_visibility_cached(Path(R1), R1, None, False, "")
    check("local_only_repo_skips_gh", vis == "" and calls == [])


def test_survives_a_settings_round_trip():
    """The cache is only worth anything if it outlives the process."""
    real_file = m._SETTINGS_FILE
    tmp = Path(tempfile.gettempdir()) / "ai-commit-vis-cache-test.json"
    m._SETTINGS_FILE = tmp
    real_dpg = m.dpg
    m.dpg = SimpleNamespace(get_viewport_pos=lambda: (0, 0),
                            get_viewport_width=lambda: 800,
                            get_viewport_height=lambda: 600)
    try:
        m.app = m.AppState(visibility_cache={R1: "PRIVATE", "C:/repos/r2": ""})
        m._save_settings()
        raw = json.loads(tmp.read_text())
        check("written_to_settings",
              raw.get("visibility_cache") == {R1: "PRIVATE", "C:/repos/r2": ""})
        m.app = m.AppState()
        loaded = m._load_settings()
        check("read_back",
              loaded.get("visibility_cache", {}).get(R1) == "PRIVATE")
    finally:
        m._SETTINGS_FILE = real_file
        m.dpg = real_dpg
        tmp.unlink(missing_ok=True)


def main():
    test_cache_hit_skips_gh()
    test_cached_empty_also_counts_as_answered()
    test_miss_calls_gh_and_populates_cache()
    test_session_state_wins_over_cache()
    test_force_reasks_and_overwrites()
    test_unchanged_answer_does_not_dirty_settings()
    test_no_remote_never_calls_gh()
    test_survives_a_settings_round_trip()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
