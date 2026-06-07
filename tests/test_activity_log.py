"""Tests for the activity_log module (logging core, git hook, file tailing).

Headless: imports only activity_log + ai_commit_core (no Dear PyGui), and
redirects activity_log.LOG_PATH to a temp file so the real session log is never
touched.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity_log
import ai_commit_core


def _fresh_logfile():
    """Point activity_log at a clean temp log and reset in-memory state."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    activity_log.LOG_PATH = path
    activity_log._buffer.clear()
    activity_log.set_enabled(True)
    activity_log.clear_file()
    return path


def test_log_event_appends_buffer_and_file():
    path = _fresh_logfile()
    activity_log.log_event("hello world", repo="myrepo")
    buf = activity_log.get_buffer()
    assert len(buf) == 1
    assert buf[0]["message"] == "hello world"
    assert buf[0]["repo"] == "myrepo"
    assert buf[0]["category"] == activity_log.CAT_EVENT
    # Round-trips through the file too.
    entries = activity_log.read_entries(path)
    assert len(entries) == 1
    assert entries[0]["message"] == "hello world"


def test_log_git_formats_command_and_repo():
    _fresh_logfile()
    activity_log.log_git(["status", "--porcelain"], cwd="/tmp/some/coolrepo", rc=0,
                         duration_ms=12)
    entry = activity_log.get_buffer()[-1]
    assert entry["message"] == "git status --porcelain"
    assert entry["repo"] == "coolrepo"
    assert entry["category"] == activity_log.CAT_GIT
    assert entry["rc"] == 0
    assert entry["duration_ms"] == 12
    assert entry["detail"] is None  # success → no detail


def test_log_git_failure_attaches_stderr_detail():
    _fresh_logfile()
    activity_log.log_git(["push"], cwd="/tmp/repo", rc=1,
                         stderr="fatal: no upstream configured")
    entry = activity_log.get_buffer()[-1]
    assert entry["rc"] == 1
    assert "no upstream" in entry["detail"]


def test_disabled_is_noop():
    _fresh_logfile()
    activity_log.set_enabled(False)
    activity_log.log_event("should not appear")
    assert activity_log.get_buffer() == []
    activity_log.set_enabled(True)


def test_read_entries_skips_malformed_lines():
    path = _fresh_logfile()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 1, "message": "good"}\n')
        fh.write('this is not json\n')
        fh.write('{"seq": 2, "message": "alsogood"}\n')
    entries = activity_log.read_entries(path)
    assert [e["message"] for e in entries] == ["good", "alsogood"]


def test_clear_file_truncates():
    path = _fresh_logfile()
    activity_log.log_event("one")
    assert os.path.getsize(path) > 0
    activity_log.clear_file()
    assert os.path.getsize(path) == 0


def test_run_git_invokes_logger_hook():
    _fresh_logfile()
    captured = []

    def hook(args, cwd, rc, duration_ms, stderr):
        captured.append((list(args), rc))

    ai_commit_core.set_git_logger(hook)
    try:
        rc, out, _err = ai_commit_core.run_git(["--version"], cwd=os.getcwd())
    finally:
        ai_commit_core.set_git_logger(None)
    assert rc == 0
    assert captured and captured[0][0] == ["--version"]
    assert captured[0][1] == 0


def test_run_git_logger_failure_does_not_break_run_git():
    _fresh_logfile()

    def bad_hook(*_a, **_k):
        raise RuntimeError("boom")

    ai_commit_core.set_git_logger(bad_hook)
    try:
        rc, out, _err = ai_commit_core.run_git(["--version"], cwd=os.getcwd())
    finally:
        ai_commit_core.set_git_logger(None)
    assert rc == 0  # hook exception swallowed


def test_install_git_logger_routes_into_activity_log():
    _fresh_logfile()
    activity_log.install_git_logger()
    try:
        ai_commit_core.run_git(["--version"], cwd=os.getcwd())
    finally:
        ai_commit_core.set_git_logger(None)
    msgs = [e["message"] for e in activity_log.get_buffer()]
    assert any(m == "git --version" for m in msgs)


def test_tailer_incremental_read():
    path = _fresh_logfile()
    tailer = activity_log.FileTailer(path)
    assert tailer.read_new() == []  # empty file
    activity_log.log_event("first")
    new = tailer.read_new()
    assert len(new) == 1 and new[0]["message"] == "first"
    # No new lines → nothing returned.
    assert tailer.read_new() == []
    activity_log.log_event("second")
    activity_log.log_event("third")
    new = tailer.read_new()
    assert [e["message"] for e in new] == ["second", "third"]


def test_tailer_resets_on_truncation():
    path = _fresh_logfile()
    tailer = activity_log.FileTailer(path)
    activity_log.log_event("before")
    assert len(tailer.read_new()) == 1
    activity_log.clear_file()  # truncate → offset must reset
    activity_log.log_event("after-reset")
    new = tailer.read_new()
    assert len(new) == 1 and new[0]["message"] == "after-reset"


def test_tailer_ignores_partial_trailing_line():
    path = _fresh_logfile()
    tailer = activity_log.FileTailer(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 1, "message": "complete"}\n')
        fh.write('{"seq": 2, "message": "partial-no-newline"')  # no \n
    new = tailer.read_new()
    assert [e["message"] for e in new] == ["complete"]
    # Now finish the partial line; it should be picked up next call.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('}\n')
    new = tailer.read_new()
    assert [e["message"] for e in new] == ["partial-no-newline"]


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
