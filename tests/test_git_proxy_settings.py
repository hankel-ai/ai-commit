"""The Git Proxy settings take effect live when the Settings window changes them.

Real Dear PyGui item creation (context only, no viewport) plus a real server:
the proxy module's own suite proves the protocol works; this proves a "set"
message from the Settings window (settings_window.py, covered by
test_settings_window.py) really starts/stops the server and reports status.

Run: python tests/test_git_proxy_settings.py
"""
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(_ROOT, "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

import dearpygui.dearpygui as dpg

import git_proxy

_failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _setup_context():
    dpg.create_context()
    m.green_btn_theme = m.create_button_theme((50, 130, 75))


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_defaults_are_off():
    check("default_disabled", m.AppState().git_proxy_enabled is False)
    check("default_port", m.AppState().git_proxy_port == git_proxy.DEFAULT_PORT)


def test_port_clamping():
    check("clamp_junk", m._clamp_proxy_port("nope") == git_proxy.DEFAULT_PORT)
    check("clamp_none", m._clamp_proxy_port(None) == git_proxy.DEFAULT_PORT)
    check("clamp_privileged", m._clamp_proxy_port(80) == git_proxy.DEFAULT_PORT)
    check("clamp_too_high", m._clamp_proxy_port(70000) == git_proxy.DEFAULT_PORT)
    check("clamp_valid", m._clamp_proxy_port("9000") == 9000)


def test_settings_status_reads_off_when_disabled():
    m.app.git_proxy_enabled = False
    m._apply_git_proxy()
    status = m._settings_status()["git_proxy"]
    check("status_reads_off_when_disabled", status == {"text": "Off", "url": ""})


def test_toggle_starts_and_stops_the_server():
    """The Settings window's checkbox arrives as a "set" message."""
    port = _free_port()
    m.app.git_proxy_port = port
    sent = []
    real_send = m._settings_send
    m._settings_send = sent.append
    try:
        m._handle_settings_msg({"type": "set", "key": "git_proxy_enabled", "value": True})
        check("app_state_enabled", m.app.git_proxy_enabled is True)
        check("server_running", m._git_proxy is not None and m._git_proxy.is_running)
        check("bound_requested_port",
              m._git_proxy is not None and m._git_proxy.port == port)
        pushed = [msg["status"]["git_proxy"] for msg in sent
                  if msg.get("type") == "status" and "git_proxy" in msg["status"]]
        check("status_sent_to_window", pushed and "http://" in pushed[-1]["text"]
              and pushed[-1]["url"].startswith("http://"))

        probe = socket.socket()
        probe.settimeout(5)
        try:
            probe.connect(("127.0.0.1", port))
            check("port_accepting", True)
        except OSError:
            check("port_accepting", False)
        finally:
            probe.close()

        m._handle_settings_msg({"type": "set", "key": "git_proxy_enabled", "value": False})
        check("app_state_disabled", m.app.git_proxy_enabled is False)
        check("server_stopped", not m._git_proxy.is_running)
        check("status_reads_off", sent[-1]["status"]["git_proxy"]["text"] == "Off")

        probe = socket.socket()
        probe.settimeout(5)
        try:
            probe.connect(("127.0.0.1", port))
            check("port_closed_after_disable", False)
            probe.close()
        except OSError:
            check("port_closed_after_disable", True)
    finally:
        m._settings_send = real_send


def test_port_change_rebinds_while_enabled():
    first, second = _free_port(), _free_port()
    m.app.git_proxy_port = first
    m.app.git_proxy_enabled = True
    try:
        m._apply_git_proxy()
        check("bound_first_port", m._git_proxy.port == first)
        m.app.git_proxy_port = second
        m._apply_git_proxy()
        check("rebound_to_second_port", m._git_proxy.port == second)
        probe = socket.socket()
        probe.settimeout(5)
        try:
            probe.connect(("127.0.0.1", first))
            check("old_port_released", False)
            probe.close()
        except OSError:
            check("old_port_released", True)
    finally:
        m.app.git_proxy_enabled = False
        m._apply_git_proxy()


def test_serves_the_live_watched_folders():
    """The provider is a closure over app.watched_folders, not a startup copy."""
    root = Path(tempfile.mkdtemp(prefix="gitproxy-settings-"))
    try:
        m.app.watched_folders = [root]
        m.app.git_proxy_port = _free_port()
        m.app.git_proxy_enabled = True
        m._apply_git_proxy()
        check("running_for_live_folders", m._git_proxy.is_running)
        check("no_repos_yet", m._git_proxy.repo_index.entries() == [])

        repo = root / "demo"
        repo.mkdir()
        import subprocess
        for args in (["init"], ["config", "user.email", "t@e.com"],
                     ["config", "user.name", "T"],
                     ["config", "commit.gpgsign", "false"]):
            subprocess.run(["git"] + args, cwd=repo, capture_output=True)
        (repo / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo,
                       capture_output=True)

        m._git_proxy.repo_index.refresh()
        slugs = [slug for slug, _p in m._git_proxy.repo_index.entries()]
        check("picks_up_new_repo", slugs == ["demo"])
    finally:
        m.app.git_proxy_enabled = False
        m._apply_git_proxy()
        shutil.rmtree(root, ignore_errors=True)


def main():
    try:
        _setup_context()
    except Exception as exc:  # no GPU/display available
        print("SKIP: cannot create a Dear PyGui context here (%s)" % exc)
        return
    # Settings writes go to the real settings file otherwise.
    m._save_settings = lambda: None
    m.app.watched_folders = [Path(_ROOT)]
    try:
        test_defaults_are_off()
        test_port_clamping()
        test_settings_status_reads_off_when_disabled()
        test_toggle_starts_and_stops_the_server()
        test_port_change_rebinds_while_enabled()
        test_serves_the_live_watched_folders()
    finally:
        if m._git_proxy is not None:
            m._git_proxy.stop()
        dpg.destroy_context()
    if _failures:
        print("\n%d check(s) failed." % len(_failures))
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
