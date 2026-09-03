"""End-to-end tests for the read-only LAN git proxy.

Nothing is stubbed. Real repos on disk, a real HTTP server on an ephemeral port,
and the real `git` binary as the client -- a protocol bug that a mocked socket
would sail past fails here. Every test that talks to the server runs on a
watchdog thread so a hang FAILS instead of wedging the suite.

Headless: imports git_proxy / ai_commit_core only (no Dear PyGui).

    python tests/test_git_proxy.py
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import git_proxy


BUDGET = 60  # seconds any single git-over-HTTP operation may take


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

def _with_watchdog(fn, budget=BUDGET):
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
        raise AssertionError("call did not return within %ss -- it hung" % budget)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _git(args, cwd=None, check=True):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed (rc=%s): %s"
                             % (" ".join(args), proc.returncode, proc.stderr))
    return proc


def _make_repo(parent, name, filename="hello.txt", content="hello\n"):
    """A real repo with one commit. Returns its Path."""
    repo = Path(parent) / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    _git(["config", "commit.gpgsign", "false"], cwd=repo)
    (repo / filename).write_text(content, encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", "initial"], cwd=repo)
    return repo


def _commit(repo, filename, content, message):
    (Path(repo) / filename).write_text(content, encoding="utf-8")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-m", message], cwd=repo)


class Fixture:
    """A temp workspace, one or more watched folders, and a running proxy."""

    def __init__(self, folder_count=1):
        self.root = Path(tempfile.mkdtemp(prefix="gitproxy-test-"))
        self.folders = []
        for i in range(folder_count):
            folder = self.root / ("watched%d" % i)
            folder.mkdir()
            self.folders.append(folder)
        self.proxy = git_proxy.GitProxy(lambda: list(self.folders))
        ok, msg = self.proxy.start(port=0, host="127.0.0.1")
        assert ok, "proxy failed to start: %s" % msg
        self.base = "http://127.0.0.1:%d" % self.proxy.port

    @property
    def folder(self):
        return self.folders[0]

    def url(self, slug):
        return "%s/%s.git" % (self.base, slug)

    def clone_dir(self, name):
        return str(self.root / name)

    def close(self):
        try:
            self.proxy.stop()
        finally:
            # git marks pack files read-only; rmtree would raise on Windows.
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# Pure units
# ---------------------------------------------------------------------------

def test_slug_disambiguates_colliding_repo_names():
    root = Path(tempfile.mkdtemp(prefix="gitproxy-slug-"))
    try:
        a = root / "alpha"
        b = root / "beta"
        a.mkdir()
        b.mkdir()
        _make_repo(a, "demo")
        _make_repo(b, "demo")
        _make_repo(a, "unique")
        index = git_proxy.RepoIndex(lambda: [a, b])
        slugs = [slug for slug, _path in index.entries()]
        assert "unique" in slugs, slugs
        assert "demo" not in slugs, "a colliding name must not claim the bare slug"
        assert "alpha/demo" in slugs and "beta/demo" in slugs, slugs
        assert index.lookup("alpha/demo") == a / "demo"
        assert index.lookup("BETA/DEMO") == b / "demo", "lookup must be case-insensitive"
        assert index.lookup("demo") is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_split_repo_path():
    assert git_proxy.split_repo_path("/demo.git/info/refs") == ("demo", "/info/refs")
    assert git_proxy.split_repo_path("/demo/info/refs") == ("demo", "/info/refs")
    assert (git_proxy.split_repo_path("/a/b.git/git-upload-pack")
            == ("a/b", "/git-upload-pack"))
    assert (git_proxy.split_repo_path("/demo.git/git-receive-pack")
            == ("demo", "/git-receive-pack"))
    assert git_proxy.split_repo_path("/demo.git/") == (None, None)
    assert git_proxy.split_repo_path("/") == (None, None)
    # Traversal shapes never resolve to a slug (lookup is map-based anyway).
    assert git_proxy.split_repo_path("/../secret/info/refs") == (None, None)
    assert git_proxy.split_repo_path("/a/../../etc/info/refs") == (None, None)
    assert git_proxy.split_repo_path("/%2e%2e/info/refs") == (None, None)
    assert git_proxy.split_repo_path("/..%5Cwin/info/refs") == (None, None)


def test_is_lan_client():
    for addr in ("127.0.0.1", "::1", "192.168.1.50", "10.1.2.3", "172.16.0.9",
                 "100.100.5.5", "169.254.7.7", "::ffff:192.168.1.50"):
        assert git_proxy.is_lan_client(addr), addr
    for addr in ("8.8.8.8", "1.1.1.1", "172.32.0.1", "2606:4700::1", "not-an-ip"):
        assert not git_proxy.is_lan_client(addr), addr


def test_pkt_line():
    assert git_proxy.pkt_line(b"# service=git-upload-pack\n") == \
        b"001e# service=git-upload-pack\n"
    assert git_proxy.pkt_line(b"") == b"0004"


# ---------------------------------------------------------------------------
# End-to-end over real HTTP
# ---------------------------------------------------------------------------

def test_clone_over_http():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo", content="from the proxy\n")
        dest = fx.clone_dir("clone")
        _with_watchdog(lambda: _git(["clone", fx.url("demo"), dest]))
        assert (Path(dest) / "hello.txt").read_text(encoding="utf-8") == \
            "from the proxy\n"


def test_clone_without_dot_git_suffix():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        dest = fx.clone_dir("clone-nosuffix")
        _with_watchdog(lambda: _git(["clone", "%s/demo" % fx.base, dest]))
        assert (Path(dest) / "hello.txt").exists()


def test_clone_with_protocol_v2():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        dest = fx.clone_dir("clone-v2")
        _with_watchdog(lambda: _git(
            ["-c", "protocol.version=2", "clone", fx.url("demo"), dest]))
        assert (Path(dest) / "hello.txt").exists()


def test_pull_picks_up_a_new_commit():
    with Fixture() as fx:
        repo = _make_repo(fx.folder, "demo")
        dest = fx.clone_dir("clone-pull")
        _with_watchdog(lambda: _git(["clone", fx.url("demo"), dest]))
        _commit(repo, "second.txt", "later\n", "second commit")
        _with_watchdog(lambda: _git(["pull"], cwd=dest))
        assert (Path(dest) / "second.txt").read_text(encoding="utf-8") == "later\n"
        log = _git(["log", "--oneline"], cwd=dest).stdout
        assert "second commit" in log, log


def test_a_repo_added_after_startup_is_served():
    """The repo list is the live watched-folder scan, not a startup snapshot."""
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        _with_watchdog(lambda: _git(["clone", fx.url("demo"),
                                     fx.clone_dir("clone-first")]))
        _make_repo(fx.folder, "later")
        fx.proxy.repo_index.refresh()
        _with_watchdog(lambda: _git(["clone", fx.url("later"),
                                     fx.clone_dir("clone-later")]))
        assert (Path(fx.clone_dir("clone-later")) / "hello.txt").exists()


def test_push_is_refused():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        dest = fx.clone_dir("clone-push")
        _with_watchdog(lambda: _git(["clone", fx.url("demo"), dest]))
        _git(["config", "user.email", "test@example.com"], cwd=dest)
        _git(["config", "user.name", "Test"], cwd=dest)
        _git(["config", "commit.gpgsign", "false"], cwd=dest)
        _commit(dest, "pushed.txt", "nope\n", "should not land")

        # The service advertisement itself refuses receive-pack...
        req = urllib.request.Request(
            "%s/demo.git/info/refs?service=git-receive-pack" % fx.base)
        try:
            _with_watchdog(lambda: urllib.request.urlopen(req, timeout=10))
            raise AssertionError("receive-pack advertisement must be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code

        # ...so a real push cannot succeed.
        proc = _with_watchdog(lambda: _git(["push", "origin", "HEAD"], cwd=dest,
                                           check=False))
        assert proc.returncode != 0, "push must fail:\n%s" % proc.stderr

        # And nothing reached the source repo.
        log = _git(["log", "--oneline"], cwd=fx.folder / "demo").stdout
        assert "should not land" not in log, log


def test_unknown_repo_is_404():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        req = urllib.request.Request(
            "%s/nosuchrepo.git/info/refs?service=git-upload-pack" % fx.base)
        try:
            _with_watchdog(lambda: urllib.request.urlopen(req, timeout=10))
            raise AssertionError("unknown repo must 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404, exc.code


def test_dumb_http_probe_is_refused():
    """No dumb protocol: it would hand out raw objects without upload-pack."""
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        req = urllib.request.Request("%s/demo.git/info/refs" % fx.base)
        try:
            _with_watchdog(lambda: urllib.request.urlopen(req, timeout=10))
            raise AssertionError("a dumb-HTTP probe must be refused")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, exc.code


def test_index_page_lists_clone_urls():
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        _make_repo(fx.folder, "other")
        body = _with_watchdog(
            lambda: urllib.request.urlopen(fx.base + "/", timeout=10).read()
        ).decode("utf-8")
        assert "%s/demo.git" % fx.base in body, body
        assert "%s/other.git" % fx.base in body, body
        assert "read-only" in body.lower()


def test_advertisement_shape():
    """The smart-HTTP service line and flush pkt, exactly as git expects."""
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        resp = _with_watchdog(lambda: urllib.request.urlopen(
            "%s/demo.git/info/refs?service=git-upload-pack" % fx.base, timeout=10))
        body = resp.read()
        assert resp.headers.get("Content-Type") == git_proxy.ADVERT_CONTENT_TYPE
        assert body.startswith(b"001e# service=git-upload-pack\n0000"), body[:60]
        assert b"refs/heads/" in body


def test_stop_releases_the_port():
    fx = Fixture()
    try:
        _make_repo(fx.folder, "demo")
        port = fx.proxy.port
        _with_watchdog(lambda: _git(["clone", fx.url("demo"),
                                     fx.clone_dir("clone-stop")]))
        fx.proxy.stop()
        assert not fx.proxy.is_running
        sock = socket.socket()
        sock.settimeout(5)
        try:
            sock.connect(("127.0.0.1", port))
            raise AssertionError("port %d still accepting after stop()" % port)
        except (ConnectionRefusedError, socket.timeout, OSError):
            pass
        finally:
            sock.close()
    finally:
        shutil.rmtree(fx.root, ignore_errors=True)


def test_restart_after_stop():
    """Toggling the setting off and on must not leave the server unusable."""
    with Fixture() as fx:
        _make_repo(fx.folder, "demo")
        fx.proxy.stop()
        ok, msg = fx.proxy.start(port=0, host="127.0.0.1")
        assert ok, msg
        fx.base = "http://127.0.0.1:%d" % fx.proxy.port
        _with_watchdog(lambda: _git(["clone", fx.url("demo"),
                                     fx.clone_dir("clone-restart")]))
        assert (Path(fx.clone_dir("clone-restart")) / "hello.txt").exists()


def test_start_reports_a_busy_port_instead_of_raising():
    with Fixture() as fx:
        other = git_proxy.GitProxy(lambda: list(fx.folders))
        ok, msg = other.start(port=fx.proxy.port, host="127.0.0.1")
        try:
            assert not ok, "binding an in-use port must fail cleanly"
            assert "unavailable" in msg, msg
        finally:
            other.stop()


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok  %s" % _name)
    print("All tests passed.")
