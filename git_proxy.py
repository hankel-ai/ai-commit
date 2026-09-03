"""Read-only Git Smart HTTP server: serve ai-commit's watched repos on the LAN.

Lets any git client on the local network ``clone`` / ``fetch`` / ``pull`` from the
repos ai-commit already tracks, without a GitHub round trip and without copying
folders between machines by hand::

    git clone http://<this-pc>:8418/<repo>.git

Only the **fetch** half of the protocol exists here. ``git-receive-pack`` is
never spawned, so a push is refused at the protocol level rather than by policy
-- nothing on the LAN can write into the working repos the user is about to
commit.

Stdlib only (``http.server``), so it adds no dependency to the GUI. It runs on a
daemon thread inside the Dear PyGui process and must never touch dpg state or
the settings file: those are main-thread only.
"""

import gzip
import html
import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import ai_commit_core

try:
    import activity_log
except Exception:  # pragma: no cover - the log is optional
    activity_log = None


DEFAULT_PORT = 8418

# A cold clone of a large repo over the LAN: generous, but still bounded so a
# wedged upload-pack can never hold a socket (and a git process) forever.
UPLOAD_PACK_TIMEOUT = 900
ADVERT_TIMEOUT = 60

# The fetch negotiation is small (want/have lines). 64 MB is far past anything
# legitimate and keeps a hostile body from ballooning memory.
MAX_REQUEST_BYTES = 64 * 1024 * 1024

STREAM_CHUNK = 64 * 1024

# Repo discovery is a filesystem walk plus a `git rev-parse` per candidate. A
# single clone makes 2-3 requests, so a short cache collapses those into one
# scan while still picking up a newly added folder within seconds.
INDEX_CACHE_TTL = 5.0

ADVERT_CONTENT_TYPE = "application/x-git-upload-pack-advertisement"
RESULT_CONTENT_TYPE = "application/x-git-upload-pack-result"

READ_ONLY_MESSAGE = ("This git proxy is read-only. Fetch and clone are served; "
                     "pushing is not supported.")


# ---------------------------------------------------------------------------
# Logging (optional -- the module is importable and testable without it)
# ---------------------------------------------------------------------------

def _log(message, detail=None, error=False):
    if activity_log is None:
        return
    try:
        category = activity_log.CAT_ERROR if error else activity_log.CAT_EVENT
        activity_log.log_event(message, detail=detail, category=category)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# pkt-line
# ---------------------------------------------------------------------------

PKT_FLUSH = b"0000"


def pkt_line(payload):
    """Wrap *payload* bytes in a git pkt-line (4-byte hex length prefix)."""
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


# ---------------------------------------------------------------------------
# Client address guard
# ---------------------------------------------------------------------------

# Loopback, RFC1918, link-local, unique-local, and the CGNAT range Tailscale
# hands out. Cheap belt-and-braces: there is no auth, so anything that is not
# plausibly on the local network or tailnet is refused outright.
_ALLOWED_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "127.0.0.0/8", "::1/128",
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7",
    "100.64.0.0/10",
))


def is_lan_client(addr):
    """True if *addr* is a private / loopback / tailnet address.

    Deliberately reads the **socket peer only**. ``X-Forwarded-For`` is client
    controlled and trivially spoofable, so it is never consulted -- behind a
    reverse proxy the proxy itself becomes the boundary.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return any(ip in net for net in _ALLOWED_NETWORKS)


# ---------------------------------------------------------------------------
# Repo index
# ---------------------------------------------------------------------------

class RepoIndex:
    """Maps a URL slug to a repo directory, rebuilt from the watched folders.

    A request path is only ever *looked up* in this map -- a filesystem path is
    never constructed from client input -- so path traversal is structurally
    impossible rather than filtered.
    """

    def __init__(self, folder_provider, ttl=INDEX_CACHE_TTL):
        self._folder_provider = folder_provider
        self._ttl = ttl
        self._lock = threading.Lock()
        self._slugs = {}      # slug.lower() -> Path
        self._display = []    # [(slug, Path)] sorted for the index page
        self._built_at = 0.0

    def _discover(self):
        found = []
        seen = set()
        try:
            folders = list(self._folder_provider() or [])
        except Exception:
            folders = []
        for folder in folders:
            try:
                repos = ai_commit_core.discover_repos(folder)
            except Exception:
                continue
            for repo in repos:
                key = str(repo).lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append(repo)
        found.sort(key=lambda p: str(p).lower())
        return found

    def _build(self):
        found = self._discover()
        # Two watched folders can each hold a `demo/`. Only the colliding names
        # get the longer `<parent>/<name>` form, so the common case stays short.
        counts = {}
        for repo in found:
            counts[repo.name.lower()] = counts.get(repo.name.lower(), 0) + 1
        slugs = {}
        display = []
        for repo in found:
            slug = repo.name
            if counts[repo.name.lower()] > 1:
                slug = "%s/%s" % (repo.parent.name, repo.name)
            base = slug
            n = 2
            while slug.lower() in slugs:  # same parent name too: still unique
                slug = "%s-%d" % (base, n)
                n += 1
            slugs[slug.lower()] = repo
            display.append((slug, repo))
        display.sort(key=lambda item: item[0].lower())
        return slugs, display

    def refresh(self):
        slugs, display = self._build()
        with self._lock:
            self._slugs = slugs
            self._display = display
            self._built_at = time.monotonic()

    def _ensure_fresh(self):
        with self._lock:
            fresh = self._built_at and (time.monotonic() - self._built_at) < self._ttl
        if not fresh:
            self.refresh()

    def lookup(self, slug):
        """Repo Path for *slug*, or None. Case-insensitive (Windows)."""
        if not slug:
            return None
        self._ensure_fresh()
        with self._lock:
            return self._slugs.get(slug.lower())

    def entries(self):
        """[(slug, Path)] for the index page."""
        self._ensure_fresh()
        with self._lock:
            return list(self._display)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_ENDPOINTS = ("/info/refs", "/git-upload-pack", "/git-receive-pack")


def split_repo_path(path):
    """Split a request path into ``(slug, endpoint)``, or ``(None, None)``.

    A trailing ``.git`` on the repo segment is optional, matching what every
    git client and every hosting service accepts.
    """
    for endpoint in _ENDPOINTS:
        if not path.endswith(endpoint):
            continue
        slug = unquote(path[: -len(endpoint)]).strip("/")
        if slug.lower().endswith(".git"):
            slug = slug[:-4]
        if not slug:
            return None, None
        parts = slug.replace("\\", "/").split("/")
        if any(part in ("", ".", "..") for part in parts):
            return None, None
        return "/".join(parts), endpoint
    return None, None


# ---------------------------------------------------------------------------
# git upload-pack
# ---------------------------------------------------------------------------

def _popen_kwargs():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _upload_pack_env(git_protocol):
    env = ai_commit_core._git_env()
    if git_protocol:
        env["GIT_PROTOCOL"] = git_protocol
    return env


def advertise_refs(repo, git_protocol=""):
    """Run ``upload-pack --advertise-refs``. Returns ``(rc, stdout, stderr)``."""
    proc = subprocess.Popen(
        ["git", "upload-pack", "--stateless-rpc", "--advertise-refs", str(repo)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_upload_pack_env(git_protocol),
        **_popen_kwargs()
    )
    try:
        out, err = proc.communicate(timeout=ADVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        ai_commit_core._kill_git_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        return ai_commit_core.GIT_TIMEOUT_RC, b"", "upload-pack --advertise-refs timed out"
    return proc.returncode, out, (err or b"").decode("utf-8", "replace")


def spawn_upload_pack(repo, git_protocol=""):
    """Start ``upload-pack --stateless-rpc`` with pipes on all three streams."""
    return subprocess.Popen(
        ["git", "upload-pack", "--stateless-rpc", str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_upload_pack_env(git_protocol),
        **_popen_kwargs()
    )


def _drain(stream, sink):
    """Read a pipe to EOF into *sink*.

    Runs on its own thread: reading stderr only after the stdout loop finishes
    would deadlock the moment git wrote more than a pipe buffer of diagnostics.
    """
    try:
        sink.append(stream.read() or b"")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class GitProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ai-commit-git-proxy"
    sys_version = ""

    # -- plumbing ---------------------------------------------------------

    @property
    def repo_index(self):
        return self.server.repo_index

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler logs to stderr, which goes nowhere under
        # pythonw.exe. Meaningful events are logged explicitly instead.
        pass

    def log_error(self, fmt, *args):
        pass

    def _no_cache(self):
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "Fri, 01 Jan 1980 00:00:00 GMT")

    def _send_plain(self, code, message):
        body = (message + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(body)

    def _git_protocol(self):
        value = (self.headers.get("Git-Protocol") or "").strip()
        # Only pass through the one value we understand; it lands in a child's
        # environment, so it is never echoed back unvalidated.
        return "version=2" if value == "version=2" else ""

    def _client_allowed(self):
        addr = self.client_address[0] if self.client_address else ""
        if is_lan_client(addr):
            return True
        _log("git-proxy: refused non-private client", detail=str(addr), error=True)
        self._send_plain(403, "Forbidden: this git proxy serves private networks only.")
        self.close_connection = True
        return False

    def _base_url(self):
        host = self.headers.get("Host")
        if not host:
            addr = self.server.server_address
            host = "%s:%s" % (addr[0], addr[1])
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        if proto not in ("http", "https"):
            proto = "http"
        return "%s://%s" % (proto, host)

    # -- GET --------------------------------------------------------------

    def do_GET(self):
        if not self._client_allowed():
            return
        parsed = urlparse(self.path)
        if parsed.path in ("", "/", "/index.html"):
            self._send_index()
            return
        slug, endpoint = split_repo_path(parsed.path)
        if endpoint != "/info/refs":
            self._send_plain(404, "Not found.")
            return
        service = parse_qs(parsed.query).get("service", [""])[0]
        if service == "git-receive-pack":
            _log("git-proxy: push refused", detail=str(slug))
            self._send_plain(403, READ_ONLY_MESSAGE)
            return
        if service != "git-upload-pack":
            # No dumb-HTTP fallback: it would serve raw object files without
            # ever going through upload-pack.
            self._send_plain(403, "Smart HTTP only (service=git-upload-pack).")
            return
        repo = self.repo_index.lookup(slug)
        if repo is None:
            self._send_plain(404, "No such repo: %s" % slug)
            return
        rc, out, err = advertise_refs(repo, self._git_protocol())
        if rc != 0:
            _log("git-proxy: advertise-refs failed", detail="%s rc=%s %s" % (slug, rc, err),
                 error=True)
            self._send_plain(500, "git upload-pack failed (rc=%s): %s" % (rc, err[:400]))
            return
        body = pkt_line(b"# service=git-upload-pack\n") + PKT_FLUSH + out
        self.send_response(200)
        self.send_header("Content-Type", ADVERT_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(body)

    def _send_index(self):
        entries = self.repo_index.entries()
        base = self._base_url()
        rows = []
        for slug, path in entries:
            url = "%s/%s.git" % (base, slug)
            rows.append(
                "<tr><td class=n>%s</td><td><code>%s</code></td><td class=p>%s</td></tr>"
                % (html.escape(slug), html.escape(url), html.escape(str(path)))
            )
        if not rows:
            rows.append("<tr><td colspan=3 class=p>No repos found in the watched "
                        "folders.</td></tr>")
        page = _INDEX_TEMPLATE % {
            "count": len(entries),
            "rows": "\n".join(rows),
        }
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._no_cache()
        self.end_headers()
        self.wfile.write(body)

    # -- POST -------------------------------------------------------------

    def do_POST(self):
        if not self._client_allowed():
            return
        parsed = urlparse(self.path)
        slug, endpoint = split_repo_path(parsed.path)
        if endpoint == "/git-receive-pack":
            _log("git-proxy: push refused", detail=str(slug))
            self._send_plain(403, READ_ONLY_MESSAGE)
            return
        if endpoint != "/git-upload-pack":
            self._send_plain(404, "Not found.")
            return
        repo = self.repo_index.lookup(slug)
        if repo is None:
            self._send_plain(404, "No such repo: %s" % slug)
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_plain(400, str(exc))
            return
        self._stream_upload_pack(slug, repo, body)

    def _read_body(self):
        encoding = (self.headers.get("Transfer-Encoding") or "").lower().strip()
        if "chunked" in encoding:
            data = self._read_chunked()
        else:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("bad Content-Length")
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("request body too large")
            data = self.rfile.read(length) if length else b""
        if "gzip" in (self.headers.get("Content-Encoding") or "").lower():
            try:
                data = gzip.decompress(data)
            except OSError:
                raise ValueError("malformed gzip body")
        return data

    def _read_chunked(self):
        chunks = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line:
                raise ValueError("truncated chunked body")
            line = line.split(b";", 1)[0].strip()
            try:
                size = int(line, 16)
            except ValueError:
                raise ValueError("malformed chunked body")
            if size == 0:
                while True:  # trailers, up to the terminating blank line
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > MAX_REQUEST_BYTES:
                raise ValueError("request body too large")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise ValueError("truncated chunked body")
            chunks.append(chunk)
            self.rfile.read(2)  # the CRLF after each chunk
        return b"".join(chunks)

    def _stream_upload_pack(self, slug, repo, body):
        started = time.monotonic()
        try:
            proc = spawn_upload_pack(repo, self._git_protocol())
        except OSError as exc:
            self._send_plain(500, "could not start git upload-pack: %s" % exc)
            return

        # The response length is unknown and a clone can be hundreds of MB, so
        # it is streamed with Transfer-Encoding: chunked rather than buffered.
        stderr_sink = []
        drainer = threading.Thread(target=_drain, args=(proc.stderr, stderr_sink),
                                   daemon=True)
        drainer.start()
        killer = threading.Timer(UPLOAD_PACK_TIMEOUT, ai_commit_core._kill_git_tree,
                                 args=(proc,))
        killer.daemon = True
        killer.start()

        sent = 0
        wrote_header = False
        client_gone = False
        try:
            try:
                proc.stdin.write(body)
                proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

            while True:
                chunk = proc.stdout.read(STREAM_CHUNK)
                if not chunk:
                    break
                if not wrote_header:
                    self.send_response(200)
                    self.send_header("Content-Type", RESULT_CONTENT_TYPE)
                    self.send_header("Transfer-Encoding", "chunked")
                    self._no_cache()
                    self.end_headers()
                    wrote_header = True
                try:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                except OSError:
                    client_gone = True
                    break
                sent += len(chunk)

            rc = proc.wait()
        finally:
            killer.cancel()
            if client_gone or proc.poll() is None:
                ai_commit_core._kill_git_tree(proc)
            drainer.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass

        err = (stderr_sink[0] if stderr_sink else b"").decode("utf-8", "replace")
        if client_gone:
            # Half a response is already on the wire; the only honest signal
            # left is closing the connection.
            self.close_connection = True
            _log("git-proxy: client disconnected mid-fetch", detail=str(slug))
            return
        if not wrote_header:
            _log("git-proxy: upload-pack produced no output", error=True,
                 detail="%s rc=%s %s" % (slug, rc, err))
            self._send_plain(500, "git upload-pack failed (rc=%s): %s" % (rc, err[:400]))
            return
        if rc != 0:
            # Headers are already sent; truncate so the client sees a failure
            # rather than a silently short packfile.
            self.close_connection = True
            _log("git-proxy: upload-pack failed mid-stream", error=True,
                 detail="%s rc=%s %s" % (slug, rc, err))
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            self.close_connection = True
            return
        _log("git-proxy: served %s (%.1f KB in %.1fs)"
             % (slug, sent / 1024.0, time.monotonic() - started))


_INDEX_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>ai-commit git proxy</title>
<style>
body{font:14px/1.5 system-ui,Segoe UI,sans-serif;background:#1e1e23;color:#dcdce1;
     margin:0;padding:28px}
h1{font-size:18px;margin:0 0 4px}
p.sub{color:#787882;margin:0 0 20px}
table{border-collapse:collapse;width:100%%}
th,td{text-align:left;padding:6px 12px 6px 0;border-bottom:1px solid #33333c;
      vertical-align:top}
th{color:#648ce6;font-weight:600;font-size:12px;text-transform:uppercase;
   letter-spacing:.04em}
td.n{font-weight:600;white-space:nowrap}
td.p{color:#787882;font-size:12px}
code{background:#2a2a31;padding:2px 6px;border-radius:3px;
     font:13px ui-monospace,Consolas,monospace;user-select:all}
</style></head><body>
<h1>ai-commit git proxy</h1>
<p class="sub">%(count)d repo(s) served read-only. Fetch and clone only &mdash;
pushing is not supported.</p>
<table><tr><th>Repo</th><th>Clone URL</th><th>Path</th></tr>
%(rows)s
</table></body></html>
"""


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    # HTTPServer defaults this on, but on Windows SO_REUSEADDR lets a second
    # process bind a port that is already in use -- so a duplicate instance
    # would silently steal connections instead of reporting "port in use".
    allow_reuse_address = os.name != "nt"
    repo_index = None

    def handle_error(self, request, client_address):
        """Swallow the traceback socketserver would print to stderr.

        A git client closing a keep-alive connection raises ConnectionReset /
        BrokenPipe here every time; that is normal, not an error worth a
        stack trace -- and stderr goes nowhere under pythonw.exe anyway.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError, TimeoutError)):
            return
        _log("git-proxy: request failed", error=True,
             detail="%s: %s" % (type(exc).__name__, exc))


def lan_ip():
    """Best-effort LAN address of this machine, for display in the UI."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.168.1.1", 9))  # no packet is sent for UDP connect
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return ""
    finally:
        sock.close()


class GitProxy:
    """Start/stop wrapper around the HTTP server, safe to toggle repeatedly."""

    def __init__(self, folder_provider):
        self.repo_index = RepoIndex(folder_provider)
        self._server = None
        self._thread = None
        self._port = 0

    @property
    def is_running(self):
        return self._server is not None

    @property
    def port(self):
        return self._port

    def base_url(self):
        if not self.is_running:
            return ""
        return "http://%s:%d/" % (lan_ip() or "127.0.0.1", self._port)

    def local_url(self):
        if not self.is_running:
            return ""
        return "http://127.0.0.1:%d/" % self._port

    def start(self, port=DEFAULT_PORT, host="0.0.0.0"):
        """Returns ``(ok, message)``. A bind failure is reported, never raised."""
        if self._server is not None:
            return True, self.base_url()
        try:
            server = _ProxyServer((host, int(port)), GitProxyHandler)
        except OSError as exc:
            msg = "port %s unavailable: %s" % (port, exc)
            _log("git-proxy: start failed", detail=msg, error=True)
            return False, msg
        server.repo_index = self.repo_index
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.2},
            name="git-proxy", daemon=True,
        )
        self._thread.start()
        _log("git-proxy: listening on %s:%d" % (host, self._port))
        return True, self.base_url()

    def stop(self):
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        try:
            server.shutdown()  # never call this from the serving thread
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        if thread is not None:
            thread.join(timeout=5)
        _log("git-proxy: stopped (port %d)" % self._port)
        self._port = 0
