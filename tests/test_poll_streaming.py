"""A poll cycle must show each repo as it lands, not all at once at the end.

_run_poll_batch used to return only when every future had resolved, and
bg_poll_repos posted a single poll_result after that -- so one slow remote held
back the whole list. With ~26 unreachable repos and the 20s stall watchdog that
is over a minute of "..." for repos that answered in milliseconds.

The concurrency tests use real threads and a real Event, so an implementation
that collects everything before reporting FAILS (the assertion runs while the
slow repo is provably still blocked) instead of quietly passing.

Run: python tests/test_poll_streaming.py
"""
import importlib.util
import os
import sys
import threading
import time
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

_REAL_POLL_ONE = m._poll_one_repo
_REAL_REBUILD = m.rebuild_repos_ui


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _info(rp):
    return {"path": Path(rp), "entries": [], "remote_url": "",
            "github_account": "", "visibility": "", "branch": "main",
            "branch_status": "", "last_commit_msg": "", "last_commit_date": "",
            "last_commit_ts": 0.0, "ahead": 0, "behind": 0}


def _live(keys, existing=None):
    return [(k, Path(k), existing, False) for k in keys]


def _drain():
    """Empty the module's ui_queue and return the messages."""
    out = []
    while not m.ui_queue.empty():
        out.append(m.ui_queue.get_nowait())
    return out


# ---------------------------------------------------------------------------
# The point of the feature: fast repos must not wait for a slow one
# ---------------------------------------------------------------------------

def test_fast_repos_are_reported_while_a_slow_one_is_still_blocked():
    m.app = m.AppState(poll_threads=4)
    release = threading.Event()
    fast_done = threading.Event()
    reported = []
    lock = threading.Lock()

    def poll(rp, existing, repo_force, force):
        if str(rp).endswith("slow"):
            # Held until the assertions below have run.
            release.wait(timeout=10)
        return _info(rp)

    def on_result(repo_key, info):
        with lock:
            reported.append(repo_key)
            n = len(reported)
        if n == 3:
            fast_done.set()

    m._poll_one_repo = poll
    live = _live(["C:/repos/slow", "C:/repos/a", "C:/repos/b", "C:/repos/c"])
    done = threading.Event()

    def run():
        try:
            m._run_poll_batch(live, False, on_result)
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        # The three fast repos must be reported while "slow" is provably still
        # inside its poll -- release has not been set yet.
        streamed_early = fast_done.wait(timeout=10)
        still_blocked = not release.is_set()
        with lock:
            early = set(reported)
    finally:
        release.set()
        done.wait(timeout=10)
        m._poll_one_repo = _REAL_POLL_ONE

    check("fast_repos_reported_before_the_batch_finished", streamed_early)
    check("slow_repo_was_genuinely_still_running", still_blocked)
    check("all_three_fast_repos_streamed",
          early == {"C:/repos/a", "C:/repos/b", "C:/repos/c"})
    check("slow_repo_reported_eventually",
          "C:/repos/slow" in reported and len(reported) == 4)


def test_batch_still_returns_every_repo():
    """Streaming is additive -- the returned dict must be unchanged."""
    m.app = m.AppState(poll_threads=4)
    m._poll_one_repo = lambda rp, e, rf, f: _info(rp)
    try:
        out = m._run_poll_batch(_live(["C:/repos/a", "C:/repos/b"]), False,
                                lambda k, i: None)
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("batch_result_complete", set(out) == {"C:/repos/a", "C:/repos/b"})


def test_a_repo_that_raises_is_still_reported_as_no_longer_pending():
    """Otherwise its placeholder would sit on '...' for the rest of the cycle."""
    m.app = m.AppState(poll_threads=2)
    m.activity_log = SimpleNamespace(
        log_event=lambda msg, **kw: None, CAT_ERROR="error", CAT_GIT="git")
    seen = []

    def boom(rp, existing, repo_force, force):
        raise OSError("repo went away")

    m._poll_one_repo = boom
    try:
        m._run_poll_batch(_live(["C:/repos/gone"]), False,
                          lambda k, i: seen.append((k, i)))
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("failure_reported_to_the_streamer", [k for k, _ in seen] == ["C:/repos/gone"])
    check("failure_carries_no_fabricated_data", seen[0][1] is None)


# ---------------------------------------------------------------------------
# _poll_streamer: pending shrinks, and snapshots are never shared mutably
# ---------------------------------------------------------------------------

def test_pending_shrinks_with_each_arrival():
    _drain()
    pending = {"a", "b", "c"}
    stream = m._poll_streamer(pending)
    stream("a", _info("C:/repos/a"))
    stream("b", _info("C:/repos/b"))
    msgs = _drain()
    check("one_message_per_arrival", len(msgs) == 2)
    check("first_message_still_lists_two_pending", msgs[0][2] == {"b", "c"})
    check("second_message_lists_one_pending", msgs[1][2] == {"c"})
    check("delta_is_just_that_repo", set(msgs[1][1]) == {"b"})


def test_each_message_gets_its_own_pending_snapshot():
    """The posted set must not be the live one the worker keeps mutating."""
    _drain()
    pending = {"a", "b"}
    stream = m._poll_streamer(pending)
    stream("a", _info("C:/repos/a"))
    posted = _drain()[0][2]
    stream("b", _info("C:/repos/b"))
    check("earlier_snapshot_unchanged", posted == {"b"})
    check("snapshot_is_not_the_live_set", posted is not pending)


# ---------------------------------------------------------------------------
# process_queue: repaints are coalesced, and poll_result wins
# ---------------------------------------------------------------------------

class _RebuildSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, results, non_git=None, **kw):
        self.calls.append((dict(results or {}), kw))


def _prime(app_kwargs=None):
    _drain()
    m.app = m.AppState(**(app_kwargs or {}))
    spy = _RebuildSpy()
    m.rebuild_repos_ui = spy
    return spy


def test_a_burst_of_arrivals_costs_one_repaint():
    """rebuild_repos_ui tears the whole list down and rebuilds it, so one
    repaint per arriving repo would be O(n^2) on a 50-repo launch."""
    spy = _prime()
    try:
        for i in range(20):
            m._post_poll_stream({f"r{j}" for j in range(i + 1, 20)},
                                {f"r{i}": _info(f"C:/repos/r{i}")})
        m.process_queue()
    finally:
        m.rebuild_repos_ui = _REAL_REBUILD
    check("twenty_arrivals_one_repaint", len(spy.calls) == 1)
    rendered, kw = spy.calls[0]
    check("repaint_carries_every_arrival", len(rendered) == 20)
    check("repaint_keeps_headers_as_the_user_left_them",
          kw.get("preserve_open") is True)
    check("repaint_told_which_repos_are_still_pending",
          kw.get("pending") == set())


def test_repaint_is_throttled_between_drains():
    spy = _prime()
    try:
        m._post_poll_stream({"b"}, {"a": _info("C:/repos/a")})
        m.process_queue()
        first = len(spy.calls)
        # A second batch arriving immediately must not repaint again.
        m._post_poll_stream(set(), {"b": _info("C:/repos/b")})
        m.process_queue()
        throttled = len(spy.calls)
        # ...but it must not be lost either: it repaints once the window passes.
        m.app.poll_stream_last_rebuild -= (m.POLL_STREAM_INTERVAL + 0.1)
        m.process_queue()
        after = len(spy.calls)
    finally:
        m.rebuild_repos_ui = _REAL_REBUILD
    check("first_batch_repaints", first == 1)
    check("immediate_second_batch_is_throttled", throttled == 1)
    check("throttled_work_is_not_dropped", after == 2)
    check("late_repaint_has_both_repos", set(spy.calls[-1][0]) == {"a", "b"})


def test_pending_is_carried_into_the_repaint():
    spy = _prime()
    try:
        m._post_poll_stream({"b", "c"}, {"a": _info("C:/repos/a")})
        m.process_queue()
    finally:
        m.rebuild_repos_ui = _REAL_REBUILD
    check("pending_reaches_rebuild", spy.calls[0][1].get("pending") == {"b", "c"})


def test_poll_result_ends_the_cycle_and_cancels_a_pending_repaint():
    """The final payload is authoritative; a stale streaming repaint must not
    land after it and resurrect a half-finished list."""
    spy = _prime()
    m._save_settings = lambda: None
    try:
        m._post_poll_stream({"b"}, {"a": _info("C:/repos/a")})
        m.ui_queue.put(("poll_result",
                        {"a": _info("C:/repos/a"), "b": _info("C:/repos/b")},
                        {}, False))
        m.process_queue()
        calls_after = len(spy.calls)
        pending_after = set(m.app.poll_pending)
        dirty_after = m.app.poll_stream_dirty
        # Nothing queued: a later frame must not repaint again.
        m.process_queue()
        calls_later = len(spy.calls)
    finally:
        m.rebuild_repos_ui = _REAL_REBUILD
    check("one_repaint_for_the_cycle", calls_after == 1)
    check("authoritative_payload_rendered", set(spy.calls[0][0]) == {"a", "b"})
    check("pending_cleared", pending_after == set())
    check("dirty_flag_cleared", dirty_after is False)
    check("no_stale_repaint_afterwards", calls_later == 1)


def test_idle_frames_do_not_repaint():
    spy = _prime()
    try:
        m.process_queue()
        m.process_queue()
    finally:
        m.rebuild_repos_ui = _REAL_REBUILD
    check("no_repaint_without_arrivals", spy.calls == [])


# ---------------------------------------------------------------------------
# Real Dear PyGui render: a pending repo must keep a visible placeholder
# ---------------------------------------------------------------------------

def test_pending_repos_render_a_placeholder_row():
    """rebuild_repos_ui clears repos_container wholesale, so without this a
    mid-cycle repaint would erase the 'repo_loading' placeholders and repos
    would blink out of the list until their own poll returned."""
    try:
        import dearpygui.dearpygui as dpg
        dpg.create_context()
    except Exception as exc:
        print(f"skip  test_pending_repos_render_a_placeholder_row ({exc})")
        return

    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    m.orange_btn_theme = m.create_button_theme((200, 130, 30))
    m.pull_btn_theme = m.create_button_theme((200, 60, 60))
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
    m.link_btn_theme = link_theme
    for alias in ("force_pause_header_theme", "force_active_header_theme",
                  "public_header_theme"):
        with dpg.theme() as t:
            pass
        dpg.add_alias(alias, t)

    m.app = m.AppState(recent_only=False, show_non_git_folders=False)
    m._set_tray_alert = lambda *a, **kw: None
    with dpg.window() as win:
        with dpg.group(tag="repos_container"):
            pass

        m.rebuild_repos_ui({"C:/repos/done": _info("C:/repos/done")}, {},
                           pending={"C:/repos/waiting"})

        texts = []
        for child in dpg.get_item_children("repos_container", 1) or []:
            if dpg.get_item_type(child) == "mvAppItemType::mvText":
                texts.append(dpg.get_value(child))
        labels = [dpg.get_item_label(c)
                  for c in dpg.get_item_children("repos_container", 1) or []]
    dpg.delete_item(win)

    check("pending_repo_has_a_placeholder",
          any("waiting" in (t or "") and "..." in (t or "") for t in texts))
    check("resolved_repo_rendered_normally",
          any("done" in (lab or "") for lab in labels))
    check("resolved_repo_has_no_placeholder",
          not any("done" in (t or "") and "..." in (t or "") for t in texts))


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
