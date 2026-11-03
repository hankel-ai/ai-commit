"""Tests for the native (non-WSL) kiro-cli provider.

The subprocess plumbing is exercised against a *real* executable stub on disk
(a .cmd on Windows, a shell script elsewhere) rather than a patched
`subprocess.run`, so argv order, stdin delivery and exit-code handling are
verified the way they actually run. Only the model behind kiro-cli is stubbed.

Headless: imports only ai_commit_core (no Dear PyGui).
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core


IS_WIN = os.name == "nt"


def _write_stub(dirpath, name, body_win, body_sh):
    """Write an executable stub and return its absolute path."""
    if IS_WIN:
        path = os.path.join(dirpath, name + ".cmd")
        data = ("@echo off\r\n" + body_win).replace("\n", "\r\n")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(data)
    else:
        path = os.path.join(dirpath, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body_sh)
        os.chmod(path, 0o755)
    return path


def _echo_stub(dirpath):
    """Stub that records argv + stdin and prints a kiro-style reply."""
    argv = os.path.join(dirpath, "argv.txt")
    stdin = os.path.join(dirpath, "stdin.txt")
    win = (
        'echo %* > "{argv}"\n'
        'more > "{stdin}"\n'
        "echo ^> feat(kiro): native windows call\n"
    ).format(argv=argv, stdin=stdin)
    sh = (
        'echo "$@" > "{argv}"\n'
        'cat > "{stdin}"\n'
        "echo '> feat(kiro): native windows call'\n"
    ).format(argv=argv, stdin=stdin)
    return _write_stub(dirpath, "kiro-cli", win, sh), argv, stdin


# ---------------------------------------------------------------------------
# generate_message_kiro against a real process
# ---------------------------------------------------------------------------

def test_prompt_goes_in_on_stdin_and_reply_is_cleaned():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        exe, argv_file, stdin_file = _echo_stub(tmp)
        out = ai_commit_core.generate_message_kiro(
            "diff --git a/x b/x\n+MARKER_LINE", "claude-sonnet-5", exe=exe,
        )
        # Reply had kiro's "> " prefix stripped.
        assert out == "feat(kiro): native windows call", out

        # The whole prompt (system prompt + diff) reached the process's stdin.
        with open(stdin_file, encoding="utf-8") as fh:
            sent = fh.read()
        assert "MARKER_LINE" in sent
        assert ai_commit_core.SYSTEM_PROMPT.splitlines()[0] in sent

        # Model travelled as argv, and no WSL/bash wrapper is involved.
        with open(argv_file, encoding="utf-8") as fh:
            args = fh.read()
        assert "chat" in args
        assert "--no-interactive" in args
        assert "--model claude-sonnet-5" in args
        assert "wsl" not in args.lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nonzero_exit_raises_with_stderr():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        exe = _write_stub(
            tmp, "kiro-cli",
            "echo not logged in 1>&2\nexit /b 3\n",
            "echo 'not logged in' 1>&2\nexit 3\n",
        )
        try:
            ai_commit_core.generate_message_kiro("d", "m", exe=exe)
        except ai_commit_core.KiroCliError as exc:
            assert "code 3" in str(exc), exc
            assert "not logged in" in str(exc), exc
        else:
            raise AssertionError("expected KiroCliError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_output_raises():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        exe = _write_stub(tmp, "kiro-cli", "exit /b 0\n", "exit 0\n")
        try:
            ai_commit_core.generate_message_kiro("d", "m", exe=exe)
        except ai_commit_core.KiroCliError as exc:
            assert "empty output" in str(exc), exc
        else:
            raise AssertionError("expected KiroCliError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_exe_raises_kiro_error():
    missing = os.path.join(tempfile.gettempdir(), "no-such-kiro-cli-xyz.exe")
    try:
        ai_commit_core.generate_message_kiro("d", "m", exe=missing)
    except ai_commit_core.KiroCliError as exc:
        assert "kiro-cli" in str(exc)
    else:
        raise AssertionError("expected KiroCliError")


# ---------------------------------------------------------------------------
# resolve_kiro_cli
# ---------------------------------------------------------------------------

def test_env_override_wins():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    prev = os.environ.get(ai_commit_core.KIRO_CLI_EXE_ENV)
    try:
        exe = _write_stub(tmp, "kiro-cli", "exit /b 0\n", "exit 0\n")
        os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = exe
        assert ai_commit_core.resolve_kiro_cli() == exe
        # Quoted paths (as typed into a Windows env var) are accepted too.
        os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = f'"{exe}"'
        assert ai_commit_core.resolve_kiro_cli() == exe
    finally:
        if prev is None:
            os.environ.pop(ai_commit_core.KIRO_CLI_EXE_ENV, None)
        else:
            os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = prev
        shutil.rmtree(tmp, ignore_errors=True)


def test_env_override_pointing_nowhere_says_so():
    prev = os.environ.get(ai_commit_core.KIRO_CLI_EXE_ENV)
    os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = r"C:\nope\kiro-cli.exe"
    try:
        ai_commit_core.resolve_kiro_cli()
    except ai_commit_core.KiroCliError as exc:
        assert "does not exist" in str(exc), exc
    else:
        raise AssertionError("expected KiroCliError")
    finally:
        if prev is None:
            os.environ.pop(ai_commit_core.KIRO_CLI_EXE_ENV, None)
        else:
            os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = prev


def _with_env_override(value, body):
    prev = os.environ.get(ai_commit_core.KIRO_CLI_EXE_ENV)
    if value is None:
        os.environ.pop(ai_commit_core.KIRO_CLI_EXE_ENV, None)
    else:
        os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = value
    try:
        body()
    finally:
        if prev is None:
            os.environ.pop(ai_commit_core.KIRO_CLI_EXE_ENV, None)
        else:
            os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = prev


def test_configured_path_wins_over_env():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        configured = _write_stub(tmp, "configured", "exit /b 0\n", "exit 0\n")
        from_env = _write_stub(tmp, "from-env", "exit /b 0\n", "exit 0\n")

        def body():
            assert ai_commit_core.resolve_kiro_cli(configured) == configured
            # Quotes arrive when a path is pasted from Explorer's "Copy as path".
            assert ai_commit_core.resolve_kiro_cli(f'"{configured}"') == configured
            # Blank means "not configured", so the old order still applies.
            assert ai_commit_core.resolve_kiro_cli("") == from_env
            assert ai_commit_core.resolve_kiro_cli("   ") == from_env

        _with_env_override(from_env, body)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_configured_path_expands_variables():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    os.environ["AI_COMMIT_TEST_DIR"] = tmp
    try:
        exe = _write_stub(tmp, "kiro-cli", "exit /b 0\n", "exit 0\n")
        typed = os.path.join("%AI_COMMIT_TEST_DIR%" if IS_WIN else "$AI_COMMIT_TEST_DIR",
                             os.path.basename(exe))
        assert ai_commit_core.resolve_kiro_cli(typed) == exe
    finally:
        os.environ.pop("AI_COMMIT_TEST_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_configured_path_missing_does_not_fall_through():
    """A typo must surface, not silently run some other kiro-cli on the machine."""
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        from_env = _write_stub(tmp, "from-env", "exit /b 0\n", "exit 0\n")
        missing = os.path.join(tmp, "nope.exe")

        def body():
            try:
                ai_commit_core.resolve_kiro_cli(missing)
            except ai_commit_core.KiroCliError as exc:
                assert missing in str(exc), exc
                assert "does not exist" in str(exc), exc
            else:
                raise AssertionError("expected KiroCliError")

        _with_env_override(from_env, body)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_generate_message_runs_the_configured_exe():
    tmp = tempfile.mkdtemp(prefix="kiro-stub-")
    try:
        exe, _argv, _stdin = _echo_stub(tmp)
        out = ai_commit_core.generate_message(
            "diff", {"provider": "kiro", "model": "m", "url": "", "kiro_cli": exe})
        assert out == "feat(kiro): native windows call", out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_candidates_are_absolute_windows_locations():
    if not IS_WIN:
        print("skip  test_candidates_are_absolute_windows_locations (not Windows)")
        return
    cands = [str(c) for c in ai_commit_core._kiro_cli_candidates()]
    assert cands, "no candidate paths"
    assert all(os.path.isabs(c) for c in cands), cands
    assert all(c.lower().endswith("kiro-cli.exe") for c in cands), cands
    local = os.environ.get("LOCALAPPDATA", "").lower()
    if local:
        assert any(c.lower().startswith(local) for c in cands), cands


# ---------------------------------------------------------------------------
# clean_kiro_output
# ---------------------------------------------------------------------------

def test_clean_strips_ansi_and_prefix():
    raw = "\x1b[32m> feat(x): thing\x1b[0m\n>\n> body line"
    assert ai_commit_core.clean_kiro_output(raw) == "feat(x): thing\n\nbody line"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
