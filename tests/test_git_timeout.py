"""Tests that no git command can block the app forever.

Regression test for the startup freeze: the first poll fetches every new repo,
and against a remote that accepts the connection but never answers,
`git fetch --prune --quiet` blocked indefinitely. _run_poll_batch joins all its
futures, so one stuck fetch meant poll_result was never posted and every repo
sat on "..." -- and that poll worker was gone for good.

Nothing is mocked. A real socket server accepts the TCP connection and never
replies (exactly what a black-holing proxy or a down VPN gateway does), a real
git fetches from it, and the test asserts the call comes back. Every test runs
on a watchdog thread so a regression FAILS instead of hanging the suite.

Headless: imports only ai_commit_core (no Dear PyGui).
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class BlackHole:
    """Accepts connections and never answers -- the hang this suite guards."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._held = []
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                conn, _addr = self._srv.accept()
            except OSError:
                return
            self._held.append(conn)  # never read, never reply, never close

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass


def _make_repo(remote_url):
    repo = tempfile.mkdtemp(prefix="ai-commit-hang-")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=repo,
                   check=True, capture_output=True)
    return repo


def _with_watchdog(fn, budget):
    """Run fn() on a thread; return its value, or raise if it outlives budget."""
    box = {}
    done = threading.Event()

    def go():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=go, daemon=True).start()
    if not done.wait(budget):
        raise AssertionError(
            f"call did not return within {budget}s -- the hang is back"
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


# ---------------------------------------------------------------------------
# The hang
# ---------------------------------------------------------------------------

def test_fetch_from_black_hole_returns_instead_of_hanging():
    hole = BlackHole()
    repo = _make_repo(f"http://127.0.0.1:{hole.port}/x.git")
    prev = os.environ.get("GIT_HTTP_LOW_SPEED_TIME")
    os.environ["GIT_HTTP_LOW_SPEED_TIME"] = "3"  # same watchdog, faster test
    try:
        started = time.perf_counter()
        rc, _out, err = _with_watchdog(
            lambda: ai_commit_core.run_git(
                ["fetch", "--prune", "--quiet"], cwd=repo, timeout=30),
            budget=45,
        )
        elapsed = time.perf_counter() - started
        assert rc != 0, "a black-holed fetch must not report success"
        assert elapsed < 30, f"took {elapsed:.1f}s -- the stall watchdog never fired"
        assert err.strip(), "failure must carry an explanation for the activity log"
    finally:
        if prev is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_TIME", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_TIME"] = prev
        hole.close()
        shutil.rmtree(repo, ignore_errors=True)


def test_hard_timeout_fires_when_gits_own_watchdog_cannot():
    """The backstop, with git's stall watchdog switched off.

    This is the layer that plain subprocess.run(timeout=) does not provide: git
    runs the transport as a `git remote-http` grandchild holding our stdout and
    stderr pipes, so killing only git leaves communicate() blocked on pipes that
    never close. The whole process tree has to go.
    """
    hole = BlackHole()
    repo = _make_repo(f"http://127.0.0.1:{hole.port}/x.git")
    prev = os.environ.get("GIT_HTTP_LOW_SPEED_LIMIT")
    os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = "0"  # 0 disables it in curl
    try:
        started = time.perf_counter()
        rc, _out, err = _with_watchdog(
            lambda: ai_commit_core.run_git(
                ["fetch", "--prune", "--quiet"], cwd=repo, timeout=8),
            budget=40,
        )
        elapsed = time.perf_counter() - started
        assert rc == ai_commit_core.GIT_TIMEOUT_RC, f"rc={rc}"
        assert "timed out" in err, err
        assert elapsed < 25, f"kill took {elapsed:.1f}s"
    finally:
        if prev is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_LIMIT", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = prev
        hole.close()
        shutil.rmtree(repo, ignore_errors=True)


def test_repo_lock_is_released_after_a_timeout():
    """A timed-out fetch must not wedge every later command on that repo.

    _run_git holds the per-repo lock for the whole subprocess, so if the timeout
    path escaped without releasing it, one dead remote would freeze that repo
    permanently instead of just costing it one poll.
    """
    hole = BlackHole()
    repo = _make_repo(f"http://127.0.0.1:{hole.port}/x.git")
    prev = os.environ.get("GIT_HTTP_LOW_SPEED_LIMIT")
    os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = "0"
    try:
        _with_watchdog(
            lambda: ai_commit_core.run_git(
                ["fetch", "--prune", "--quiet"], cwd=repo, timeout=5),
            budget=35,
        )
        rc, out, _err = _with_watchdog(
            lambda: ai_commit_core.run_git(["rev-parse", "--show-toplevel"],
                                           cwd=repo),
            budget=20,
        )
        assert rc == 0, "repo unusable after a timeout"
        assert out.strip()
    finally:
        if prev is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_LIMIT", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = prev
        hole.close()
        shutil.rmtree(repo, ignore_errors=True)


def test_timeout_reaches_the_activity_log():
    """The old failure was invisible: the logger only fires once a command
    returns, so a hung fetch produced no log line at all while it hung."""
    hole = BlackHole()
    repo = _make_repo(f"http://127.0.0.1:{hole.port}/x.git")
    seen = []
    prev_limit = os.environ.get("GIT_HTTP_LOW_SPEED_LIMIT")
    os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = "0"
    ai_commit_core.set_git_logger(
        lambda args, cwd, rc, ms, stderr: seen.append((args, rc, stderr))
    )
    try:
        _with_watchdog(
            lambda: ai_commit_core.run_git(
                ["fetch", "--prune", "--quiet"], cwd=repo, timeout=5),
            budget=35,
        )
        assert seen, "nothing logged"
        args, rc, stderr = seen[-1]
        assert args[0] == "fetch"
        assert rc == ai_commit_core.GIT_TIMEOUT_RC
        assert "timed out" in stderr
    finally:
        ai_commit_core.set_git_logger(None)
        if prev_limit is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_LIMIT", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = prev_limit
        hole.close()
        shutil.rmtree(repo, ignore_errors=True)


def test_fetch_remote_swallows_the_failure():
    """fetch_remote is best-effort -- a dead remote must not break the poll."""
    hole = BlackHole()
    repo = _make_repo(f"http://127.0.0.1:{hole.port}/x.git")
    prev = os.environ.get("GIT_HTTP_LOW_SPEED_LIMIT")
    os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = "0"
    try:
        # Shrink the poll budget for the test rather than waiting the real one.
        real = ai_commit_core.GIT_TIMEOUT_POLL_FETCH
        ai_commit_core.GIT_TIMEOUT_POLL_FETCH = 5
        try:
            _with_watchdog(lambda: ai_commit_core.fetch_remote(repo), budget=35)
        finally:
            ai_commit_core.GIT_TIMEOUT_POLL_FETCH = real
    finally:
        if prev is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_LIMIT", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_LIMIT"] = prev
        hole.close()
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# Ordinary commands are unaffected
# ---------------------------------------------------------------------------

def test_local_commands_still_work():
    repo = _make_repo("http://127.0.0.1:1/unused.git")
    try:
        rc, out, _err = ai_commit_core.run_git(["rev-parse", "--show-toplevel"],
                                               cwd=repo)
        assert rc == 0 and out.strip()
        rc, _out, _err = ai_commit_core.run_git(["status", "--porcelain"],
                                                cwd=repo)
        assert rc == 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_binary_mode_still_returns_bytes():
    repo = _make_repo("http://127.0.0.1:1/unused.git")
    try:
        path = os.path.join(repo, "crlf.txt")
        with open(path, "wb") as fh:
            fh.write(b"one\r\ntwo\r\n")
        subprocess.run(["git", "add", "crlf.txt"], cwd=repo, check=True,
                       capture_output=True)
        rc, out, _err = ai_commit_core.run_git_bytes(
            ["diff", "--cached", "--numstat"], cwd=repo)
        assert rc == 0
        assert isinstance(out, bytes), type(out)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# Timeout selection and environment
# ---------------------------------------------------------------------------

def test_network_subcommands_get_the_longer_budget():
    assert (ai_commit_core._default_git_timeout(["fetch", "--prune"])
            == ai_commit_core.GIT_TIMEOUT_NETWORK)
    assert (ai_commit_core._default_git_timeout(["push", "-o", "x"])
            == ai_commit_core.GIT_TIMEOUT_NETWORK)
    # Leading flags must not be mistaken for the subcommand.
    assert (ai_commit_core._default_git_timeout(["-c", "x=y", "pull"])
            == ai_commit_core.GIT_TIMEOUT_NETWORK)
    assert (ai_commit_core._default_git_timeout(["status", "--porcelain"])
            == ai_commit_core.GIT_TIMEOUT_LOCAL)
    assert (ai_commit_core._default_git_timeout(["remote", "get-url", "origin"])
            == ai_commit_core.GIT_TIMEOUT_LOCAL)


def test_git_subcommand_skips_global_options():
    assert ai_commit_core.git_subcommand(["status", "--porcelain"]) == "status"
    assert ai_commit_core.git_subcommand(["-c", "core.x=1", "pull"]) == "pull"
    assert ai_commit_core.git_subcommand(["--git-dir", "/tmp/g", "fetch"]) == "fetch"
    assert ai_commit_core.git_subcommand(["--no-pager", "log"]) == "log"
    assert ai_commit_core.git_subcommand([]) == ""


def test_git_env_never_prompts():
    env = ai_commit_core._git_env()
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_HTTP_LOW_SPEED_LIMIT"] == "1"
    assert int(env["GIT_HTTP_LOW_SPEED_TIME"]) > 0
    assert "BatchMode=yes" in env["GIT_SSH_COMMAND"]


def test_git_env_respects_an_operator_override():
    prev = os.environ.get("GIT_HTTP_LOW_SPEED_TIME")
    os.environ["GIT_HTTP_LOW_SPEED_TIME"] = "99"
    try:
        assert ai_commit_core._git_env()["GIT_HTTP_LOW_SPEED_TIME"] == "99"
    finally:
        if prev is None:
            os.environ.pop("GIT_HTTP_LOW_SPEED_TIME", None)
        else:
            os.environ["GIT_HTTP_LOW_SPEED_TIME"] = prev


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
