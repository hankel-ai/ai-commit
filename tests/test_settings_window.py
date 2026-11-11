"""Settings runs as its own OS window (settings_window.py) and edits reach the app.

Two halves:

* the window's UI, built with real Dear PyGui items (context only): every
  section is there, each widget's bound callback emits the right "set"
  message, and status/values messages from the app update what is shown;
* the real thing end to end: ai-commit-gui launches the real settings process
  (a real viewport opens briefly), it fits itself to its content and reports
  "ready" over the pipe, values sent to it arrive, and closing its stdin ends
  it. Then the app side: "set" messages go through apply_setting and persist.

Run: python tests/test_settings_window.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

import dearpygui.dearpygui as dpg

import settings_window as sw

_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(_ROOT, "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


INIT = {
    "type": "init",
    "values": {
        "poll_interval": 30, "poll_threads": 8, "idle_poll_interval": 900,
        "auto_generate": True, "always_on_top": False,
        "actions_popup_enabled": True, "chime_on_completion": False,
        "run_at_startup": False, "ollama_url": "http://localhost:11434",
        "api_key": "", "kiro_cli_path": "", "git_proxy_enabled": False,
        "git_proxy_port": 9419, "identity_guard_enabled": True,
        "identity_pins": [{"host": "gitlab.example.com", "name": "W",
                           "email": "w@corp.com", "login": "jdoe"}],
        "show_non_git_folders": False, "recent_only": False, "recent_days": 14,
    },
    "status": {"kiro": {"text": "Using: C:/kiro.exe", "error": False},
               "git_proxy": {"text": "Off", "url": ""},
               "identity": {"lines": ["gitlab.example.com: W <w@corp.com> -- applied"],
                            "ok": True},
               "global_identity": "Personal <p@home.com>"},
    "anchor": {"x": 100, "y": 100, "w": 500, "h": 700},
    "limits": {"poll_threads_max": 16},
    "startup_supported": True,
}


def _all_text():
    out = []
    for item in dpg.get_all_items():
        if not dpg.does_item_exist(item):
            continue
        out.append(dpg.get_item_label(item) or "")
        if dpg.get_item_type(item) == "mvAppItemType::mvText":
            out.append(str(dpg.get_value(item) or ""))
    return "\n".join(out)


def _fire(tag, value):
    """Set a widget and run the callback DPG would run for that edit."""
    dpg.set_value(tag, value)
    cb = dpg.get_item_callback(tag)
    cb(tag, value, dpg.get_item_user_data(tag))


def _sets(sent):
    return [(msg["key"], msg["value"]) for msg in sent if msg.get("type") == "set"]


def _button(label):
    for item in dpg.get_all_items():
        if (dpg.get_item_type(item) == "mvAppItemType::mvButton"
                and dpg.get_item_label(item) == label):
            return item
    return None


def _click(label, nth=0):
    found = [i for i in dpg.get_all_items()
             if dpg.get_item_type(i) == "mvAppItemType::mvButton"
             and dpg.get_item_label(i) == label]
    btn = found[nth]
    dpg.get_item_callback(btn)(btn, None, dpg.get_item_user_data(btn))


# --- the window's UI (in-process) ------------------------------------------

def test_window_ui():
    dpg.create_context()
    try:
        sent = []
        win = sw.SettingsWindow(json.loads(json.dumps(INIT)), sent.append)
        win.build()
        blob = _all_text()
        for heading in ("Polling", "Behavior", "Provider", "Git Proxy (LAN)",
                        "Commit Identity", "Display"):
            check(f"section {heading}", heading in blob)
        check("run_at_startup_shown", dpg.does_item_exist("s_run_at_startup"))
        check("init_values", dpg.get_value("s_poll_interval") == 30
              and dpg.get_value("s_auto_generate") is True
              and dpg.get_value("s_git_proxy_port") == 9419)
        check("init_pin_row", dpg.get_value("s_pin_0_host") == "gitlab.example.com"
              and dpg.get_value("s_pin_0_login") == "jdoe")
        check("init_status_kiro", dpg.get_value("s_kiro_status") == "Using: C:/kiro.exe")
        check("init_status_identity", "applied" in dpg.get_value("s_identity_status"))
        check("init_global_identity", "Personal <p@home.com>" in
              dpg.get_value("s_global_identity"))

        _fire("s_poll_interval", 45)
        _fire("s_auto_generate", False)
        _fire("s_kiro_cli_path", '  "C:/x/kiro.exe"  ')
        _fire("s_recent_only", True)
        sets = _sets(sent)
        check("set_poll", ("poll_interval", 45) in sets)
        check("set_bool", ("auto_generate", False) in sets)
        check("set_kiro_stripped", ("kiro_cli_path", "C:/x/kiro.exe") in sets)
        check("set_recent", ("recent_only", True) in sets)

        sent.clear()
        _click("+ Add host")
        check("add_row", dpg.does_item_exist("s_pin_1_host"))
        check("add_sends_nothing_yet", _sets(sent) == [])
        _fire("s_pin_1_host", " https://me@Git.Other.ORG/x/y.git ")
        _fire("s_pin_1_email", "o@other.org")
        check("field_edit_not_sent_per_keystroke", _sets(sent) == [])
        win.send_pins()  # what the deactivated-after-edit handler calls
        pins = dict(_sets(sent)).get("identity_pins")
        check("pins_sent", pins and len(pins) == 2)
        check("pins_host_normalized", pins and pins[1]["host"] == "git.other.org")

        sent.clear()
        _click("Remove", 0)
        pins = dict(_sets(sent)).get("identity_pins")
        check("remove_sends", pins is not None and len(pins) == 1
              and pins[0]["host"] == "git.other.org")
        check("remove_rebuilds", not dpg.does_item_exist("s_pin_1_host"))

        win.handle({"type": "status", "status": {
            "git_proxy": {"text": "Serving read-only at  http://10.0.0.5:9419",
                          "url": "http://10.0.0.5:9419"},
            "identity": {"lines": ["git.other.org: not applied: x"], "ok": False}}})
        check("status_proxy", "10.0.0.5" in dpg.get_value("s_git_proxy_status"))
        check("status_identity_problem", "not applied" in dpg.get_value("s_identity_status"))
        win.handle({"type": "values", "values": {"recent_only": False,
                                                 "identity_pins": []}})
        check("values_checkbox", dpg.get_value("s_recent_only") is False)
        check("values_pins_rebuilt", not dpg.does_item_exist("s_pin_0_host"))
    finally:
        dpg.destroy_context()


def test_placement():
    area = (0, 0, 1920, 1040)
    x, y = sw._place({"x": 1400, "y": 50, "w": 500}, 600, 900, area)
    check("place_left_of_main", x == 1400 - 600 - sw.GAP and y == 50)
    x, _y = sw._place({"x": 100, "y": 50, "w": 500}, 600, 900, area)
    check("place_right_when_no_room_left", x == 100 + 500 + sw.GAP)
    _x, y = sw._place({"x": 1400, "y": 600, "w": 500}, 600, 900, area)
    check("place_clamped_on_screen", y == 1040 - 900)


# --- end to end: real process, real viewport ---------------------------------

def _drain_until(kind, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = m.ui_queue.get(timeout=0.5)
        except Exception:
            continue
        if msg[0] == "settings_msg" and msg[1].get("type") == kind:
            return msg[1]
        if msg[0] == kind:
            return msg
    return None


def test_real_process_round_trip():
    while not m.ui_queue.empty():
        m.ui_queue.get_nowait()
    m.app = m.AppState()
    m.app.identity_pins = [{"host": "gitlab.example.com", "name": "W",
                            "email": "w@corp.com", "login": ""}]
    dpg.create_context()  # the launcher reads the main viewport's position
    dpg.create_viewport(title="t", width=500, height=700, x_pos=1300, y_pos=60)
    proc = m._launch_settings()
    dpg.destroy_context()
    try:
        check("launched", proc is not None and m._settings_running())
        ready = _drain_until("ready")
        check("ready_received", ready is not None)
        if ready:
            check("fits_content", ready["client_height"] >= ready["content_height"]
                  or ready["height"] >= ready["max_height"] - 2)
            check("not_screen_tall_for_little_content",
                  ready["height"] < ready["max_height"]
                  or ready["content_height"] >= ready["max_height"] - 60)
            print(f"     size {ready['width']}x{ready['height']} "
                  f"(content {ready['content_height']}, max {ready['max_height']})")
        m._settings_send({"type": "values", "values": {"recent_only": True}})
        time.sleep(0.5)
        check("still_running_after_values", m._settings_running())
    finally:
        m._close_settings(timeout=10)
    check("exits_on_stdin_eof", proc is not None and proc.poll() is not None)
    closed = _drain_until("settings_closed", timeout=10)
    check("closed_reported", closed is not None)
    if closed:
        m._forget_settings_proc(closed[1])
    check("forgotten", not m._settings_running())


# --- app side: "set" messages take effect and persist ---------------------------

def test_apply_setting_persists():
    tmp = Path(tempfile.mkdtemp(prefix="settings-apply-"))
    original = m._SETTINGS_FILE
    m._SETTINGS_FILE = tmp / "settings.json"
    polls = []
    real_trigger, real_topmost = m.trigger_poll, m._set_topmost
    m.trigger_poll = lambda force=False: polls.append(force)
    topmost = []
    m._set_topmost = topmost.append
    dpg.create_context()
    dpg.create_viewport(title="t", width=200, height=200)  # _save_settings reads it
    try:
        m.app = m.AppState()
        m._handle_settings_msg({"type": "set", "key": "poll_interval", "value": 2})
        check("apply_clamps_low", m.app.poll_interval == 5)
        m._handle_settings_msg({"type": "set", "key": "poll_threads", "value": 99})
        check("apply_clamps_high", m.app.poll_threads == m.POLL_FANOUT_MAX)
        m._handle_settings_msg({"type": "set", "key": "poll_interval", "value": "junk"})
        check("apply_ignores_junk", m.app.poll_interval == 5)
        m._handle_settings_msg({"type": "set", "key": "always_on_top", "value": True})
        check("apply_topmost", m.app.always_on_top is True and topmost == [True])
        m._handle_settings_msg({"type": "set", "key": "ollama_url", "value": "  "})
        check("apply_blank_url_ignored", m.app.ollama_url == m.AppState().ollama_url)
        m._handle_settings_msg({"type": "set", "key": "recent_days", "value": 30})
        check("apply_recent_days_polls", m.app.recent_days == 30 and polls)
        m._handle_settings_msg({"type": "set", "key": "nonsense", "value": 1})
        saved = json.loads(m._SETTINGS_FILE.read_text())
        check("persisted", saved["poll_interval"] == 5 and saved["always_on_top"] is True
              and saved["recent_days"] == 30)
        check("unknown_key_not_saved", "nonsense" not in saved)
    finally:
        dpg.destroy_context()
        m.trigger_poll, m._set_topmost = real_trigger, real_topmost
        m._SETTINGS_FILE = original
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                check(f"{name} raised {type(exc).__name__}: {exc}", False)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
