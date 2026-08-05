"""A poll cycle must work on many repos at once, not one at a time.

bg_poll_repos is submitted to the executor as a SINGLE task, and its per-repo
loop used to be serial -- so raising the pool's max_workers did nothing at all.
With 63 watched repos that meant ~70 s before the window populated, ~68 s of it
network I/O (a `git fetch` and a `gh repo view` per repo) waited on in sequence.

These tests exercise the real ThreadPoolExecutor -- a stub that merely counts
calls would pass just as happily against the old serial loop.

Run: python tests/test_poll_parallel.py
"""
import importlib.util
import os
import sys
import threading
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


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _fake_repo(path):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", last_commit_ts=0.0,
        ahead=0, behind=0,
    )


def _live(n, existing=None):
    out = []
    for i in range(n):
        key = f"C:/repos/r{i}"
        out.append((key, Path(key), existing, False))
    return out


class _ConcurrencyProbe:
    """Stands in for _poll_one_repo and records true peak overlap.

    Every call waits on a barrier sized to the number of workers we expect, so
    a serial implementation would deadlock rather than quietly pass -- the
    timeout turns that into a failed assertion instead of a hung test.
    """

    def __init__(self, expect_parallel):
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.calls = []
        self.barrier = threading.Barrier(expect_parallel, timeout=10)

    def __call__(self, rp, existing, repo_force, force):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.calls.append(str(rp))
        try:
            self.barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with self.lock:
            self.inflight -= 1
        return {"path": rp, "entries": [], "remote_url": "",
                "github_account": "", "visibility": "", "branch": "main",
                "branch_status": "", "last_commit_msg": "",
                "last_commit_date": "", "last_commit_ts": 0.0,
                "ahead": 0, "behind": 0}


def test_repos_are_polled_concurrently():
    m.app = m.AppState(poll_threads=4)
    probe = _ConcurrencyProbe(expect_parallel=4)
    m._poll_one_repo = probe
    try:
        out = m._run_poll_batch(_live(4), False)
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("peak_overlap_is_the_full_width", probe.peak == 4)
    check("every_repo_returned", len(out) == 4)
    check("keyed_by_repo_key", set(out) == {f"C:/repos/r{i}" for i in range(4)})


def test_width_is_capped_by_the_setting():
    """8 repos, poll_threads=2 -> never more than 2 at once."""
    m.app = m.AppState(poll_threads=2)
    probe = _ConcurrencyProbe(expect_parallel=2)
    m._poll_one_repo = probe
    try:
        out = m._run_poll_batch(_live(8), False)
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("peak_respects_poll_threads", probe.peak <= 2)
    check("peak_actually_reached_two", probe.peak == 2)
    check("all_eight_still_polled", len(out) == 8)


def test_width_never_exceeds_the_work():
    """2 repos with poll_threads=8 must not spin up 8 threads."""
    m.app = m.AppState(poll_threads=8)
    seen = {}
    real_pool = m.ThreadPoolExecutor

    def spy_pool(max_workers=None, **kw):
        seen["max_workers"] = max_workers
        return real_pool(max_workers=max_workers, **kw)

    m.ThreadPoolExecutor = spy_pool
    m._poll_one_repo = _ConcurrencyProbe(expect_parallel=2)
    try:
        m._run_poll_batch(_live(2), False)
    finally:
        m.ThreadPoolExecutor = real_pool
        m._poll_one_repo = _REAL_POLL_ONE
    check("workers_clamped_to_work", seen.get("max_workers") == 2)


def test_empty_batch_starts_no_pool():
    m.app = m.AppState(poll_threads=8)
    started = []
    real_pool = m.ThreadPoolExecutor
    m.ThreadPoolExecutor = lambda *a, **kw: started.append(1) or real_pool(*a, **kw)
    try:
        out = m._run_poll_batch([], False)
    finally:
        m.ThreadPoolExecutor = real_pool
    check("no_pool_for_empty_batch", started == [])
    check("empty_batch_empty_result", out == {})


def test_one_failing_repo_does_not_lose_the_others():
    """The old serial loop let an exception kill the whole cycle: no
    poll_result was ever posted and the UI stayed on '...' forever."""
    m.app = m.AppState(poll_threads=4)
    logged = []
    m.activity_log = SimpleNamespace(
        log_event=lambda msg, **kw: logged.append(msg),
        CAT_ERROR="error", CAT_GIT="git",
    )

    def boom(rp, existing, repo_force, force):
        if str(rp).endswith("r1"):
            raise OSError("repo went away")
        return {"path": rp, "entries": [], "branch": "main"}

    m._poll_one_repo = boom
    try:
        live = [(k, p, e, f) for k, p, e, f in _live(3)]
        # r1 has prior state to fall back on; r0/r2 succeed.
        live[1] = (live[1][0], live[1][1], _fake_repo("C:/repos/r1"), False)
        out = m._run_poll_batch(live, False)
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("survivors_kept", {"C:/repos/r0", "C:/repos/r2"} <= set(out))
    check("failure_falls_back_to_cache", out.get("C:/repos/r1", {}).get("branch") == "main")
    check("failure_logged", any("Poll failed" in x for x in logged))


def test_unknown_repo_failure_is_dropped_not_faked():
    """A brand-new repo that fails has no cache to fall back on -- it must be
    absent from the results, not invented."""
    m.app = m.AppState(poll_threads=2)
    m.activity_log = SimpleNamespace(
        log_event=lambda msg, **kw: None, CAT_ERROR="error", CAT_GIT="git")

    def boom(rp, existing, repo_force, force):
        raise OSError("nope")

    m._poll_one_repo = boom
    try:
        out = m._run_poll_batch(_live(2), False)
    finally:
        m._poll_one_repo = _REAL_POLL_ONE
    check("new_repo_failure_omitted", out == {})


def test_discovery_probes_in_parallel():
    """is_git_repo across a folder's children is ~1 git spawn each -- 71 of
    them / ~3.8 s before anything else can start."""
    m.app = m.AppState(poll_threads=4)
    lock = threading.Lock()
    state = {"inflight": 0, "peak": 0}
    barrier = threading.Barrier(4, timeout=10)

    def spy(p):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with lock:
            state["inflight"] -= 1
        return str(p).endswith(("a", "c"))

    real = m.is_git_repo
    m.is_git_repo = spy
    try:
        flags = m._map_is_git_repo([Path(f"C:/x/{n}") for n in "abcd"])
    finally:
        m.is_git_repo = real
    check("discovery_ran_in_parallel", state["peak"] == 4)
    check("discovery_order_preserved", flags == [True, False, True, False])


def test_discovery_survives_an_unreadable_folder():
    m.app = m.AppState(poll_threads=2)
    real = m.is_git_repo

    def spy(p):
        if str(p).endswith("bad"):
            raise OSError("access denied")
        return True

    m.is_git_repo = spy
    try:
        flags = m._map_is_git_repo([Path("C:/x/ok"), Path("C:/x/bad")])
    finally:
        m.is_git_repo = real
    check("unreadable_folder_is_not_a_repo", flags == [True, False])


def main():
    test_repos_are_polled_concurrently()
    test_width_is_capped_by_the_setting()
    test_width_never_exceeds_the_work()
    test_empty_batch_starts_no_pool()
    test_one_failing_repo_does_not_lose_the_others()
    test_unknown_repo_failure_is_dropped_not_faked()
    test_discovery_probes_in_parallel()
    test_discovery_survives_an_unreadable_folder()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
