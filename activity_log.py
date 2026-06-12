"""Central activity log for ai-commit.

Captures the git commands run under the hood plus high-level app events, writes
them as JSON Lines to a well-known file, and keeps an in-memory ring buffer.
The separate ``activity_log_viewer.py`` window tails that JSONL file so the user
can watch everything the app is doing — and every git command it runs — in real
time.

Dependency-light by design: imports only the standard library so it can be unit
tested headless and imported by both the GUI and ``ai_commit_core`` without
pulling in Dear PyGui.
"""

import json
import re
import tempfile
import threading
import time
from pathlib import Path

# Well-known log file shared between the app process and the viewer subprocess.
LOG_PATH = Path(tempfile.gettempdir()) / "ai-commit-activity.jsonl"

# Entry categories.
CAT_GIT = "git"
CAT_EVENT = "event"
CAT_AI = "ai"
CAT_ERROR = "error"

_MAX_BUFFER = 2000

# URL userinfo (https://user:token@host or https://token@host). Git echoes the
# remote URL in push/fetch errors, so PAT-embedded remotes would otherwise
# land verbatim in the on-disk log.
_URL_CREDENTIAL_RE = re.compile(r"://[^/@\s]+@")


def redact_credentials(text):
    """Mask credentials embedded in URLs before the text is persisted."""
    if not text:
        return text
    return _URL_CREDENTIAL_RE.sub("://***@", text)

_lock = threading.Lock()
_buffer = []        # in-memory ring buffer of entry dicts
_listeners = []     # in-process callbacks: cb(entry)
_enabled = True
_seq = 0


def set_enabled(value):
    """Enable or disable logging globally. When disabled, calls are no-ops."""
    global _enabled
    _enabled = bool(value)


def is_enabled():
    return _enabled


def _next_seq():
    global _seq
    _seq += 1
    return _seq


def _make_entry(category, message, repo=None, detail=None, rc=None,
                duration_ms=None):
    return {
        "seq": _next_seq(),
        "ts": time.time(),
        "category": category,
        "repo": repo,
        "message": redact_credentials(message),
        "detail": redact_credentials(detail),
        "rc": rc,
        "duration_ms": duration_ms,
    }


def log_entry(entry):
    """Append a pre-built entry to the buffer + file and notify listeners."""
    if not _enabled:
        return
    with _lock:
        _buffer.append(entry)
        if len(_buffer) > _MAX_BUFFER:
            del _buffer[: len(_buffer) - _MAX_BUFFER]
        listeners = list(_listeners)
    _append_to_file(entry)
    for cb in listeners:
        try:
            cb(entry)
        except Exception:
            pass


def log_event(message, repo=None, detail=None, category=CAT_EVENT):
    """Log a high-level app event (poll, generate, commit, push, ...)."""
    entry = _make_entry(category, message, repo=repo, detail=detail)
    log_entry(entry)
    return entry


def log_git(args, cwd, rc=None, duration_ms=None, stderr=None):
    """Log a single git invocation. Matches ``run_git``'s logging hook signature.

    Builds a human-readable ``git ...`` line, tags it with the repo folder name,
    and on a non-zero return code attaches the first chunk of stderr as detail.
    """
    repo = Path(str(cwd)).name if cwd else None
    cmd = "git " + " ".join(str(a) for a in args)
    detail = None
    if rc not in (None, 0) and stderr:
        detail = stderr.strip()[:500]
    entry = _make_entry(
        CAT_GIT, cmd, repo=repo, detail=detail, rc=rc, duration_ms=duration_ms,
    )
    log_entry(entry)
    return entry


def _append_to_file(entry):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def add_listener(cb):
    """Register an in-process callback invoked with each new entry."""
    with _lock:
        _listeners.append(cb)


def remove_listener(cb):
    with _lock:
        if cb in _listeners:
            _listeners.remove(cb)


def get_buffer():
    """Return a snapshot copy of the in-memory ring buffer."""
    with _lock:
        return list(_buffer)


def clear_file():
    """Truncate the on-disk log — call once at app startup for a fresh session."""
    try:
        Path(LOG_PATH).write_text("", encoding="utf-8")
    except OSError:
        pass


def read_entries(path=None):
    """Read all entries from a JSONL log file. Used by the viewer on startup.

    Skips malformed lines so a partially written tail line never crashes the
    reader.
    """
    path = Path(path) if path else LOG_PATH
    entries = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except (ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        pass
    return entries


class FileTailer:
    """Incrementally reads appended JSONL entries from a log file.

    Tracks the byte offset already consumed so each ``read_new`` call returns
    only entries written since the previous call. If the file shrinks (the app
    truncated it for a new session via :func:`clear_file`) the offset resets so
    the reader reloads from the top. A partially written final line (no trailing
    newline) is left unconsumed and re-read whole on the next call.

    Lives here (dependency-light) rather than in the Dear PyGui viewer so the
    tail logic can be unit tested headless.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._offset = 0  # byte offset already consumed

    def read_new(self):
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0  # file shrank — truncated, start over
        try:
            with open(self.path, "rb") as fh:
                # Validate the resume point is still a line boundary. If the file
                # was truncated and regrown past our offset (app restarted while
                # the viewer was open), the previous byte won't be a newline, so
                # restart from the top to avoid reading a mid-line fragment.
                if self._offset > 0:
                    fh.seek(self._offset - 1)
                    if fh.read(1) != b"\n":
                        self._offset = 0
                fh.seek(self._offset)
                data = fh.read()
        except OSError:
            return []

        entries = []
        start = 0
        while True:
            nl = data.find(b"\n", start)
            if nl == -1:
                break  # partial trailing line — re-read whole next call
            raw = data[start:nl]
            self._offset += (nl - start) + 1
            start = nl + 1
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                entries.append(json.loads(text))
            except (ValueError, json.JSONDecodeError):
                continue
        return entries


def install_git_logger():
    """Wire activity logging into ``ai_commit_core.run_git``.

    Safe to call once at GUI startup. Keeps the core decoupled — it only knows
    about an optional callback, not about this module.
    """
    import ai_commit_core
    ai_commit_core.set_git_logger(log_git)
