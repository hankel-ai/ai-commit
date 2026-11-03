"""The Kiro CLI path is set in the Settings window and used by Generate.

Real Dear PyGui items, a real settings file, a real git repo and a real executable
stub. Only the model behind kiro-cli is stubbed. The environment variable points at
a stub that fails, so a Generate that ignored the setting would report the failure.

Run: python tests/test_kiro_setting.py
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_spec = importlib.util.spec_from_file_location("acg", os.path.join(ROOT, "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

import dearpygui.dearpygui as dpg

import ai_commit_core

_failures = []
IS_WIN = os.name == "nt"


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def write_stub(folder, name, reply, exit_code=0):
    """Executable that drains stdin, prints *reply* and exits with *exit_code*."""
    if IS_WIN:
        path = os.path.join(folder, name + ".cmd")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(f"@echo off\r\nmore >nul\r\necho {reply}\r\nexit /b {exit_code}\r\n")
    else:
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\ncat >/dev/null\necho '{reply}'\nexit {exit_code}\n")
        os.chmod(path, 0o755)
    return path


def open_settings():
    if dpg.does_item_exist("settings_window"):
        dpg.delete_item("settings_window")
    m.cb_open_settings(None, None)


def type_path(value):
    dpg.set_value("settings_kiro_cli", value)
    m.cb_kiro_cli_changed("settings_kiro_cli", value)


def status():
    return dpg.get_value("settings_kiro_status")


def test_field_shows_the_saved_path_and_what_it_resolves_to(tmp):
    good = write_stub(tmp, "good", "x")
    m.app.kiro_cli_path = good
    open_settings()
    check("field shows the saved path", dpg.get_value("settings_kiro_cli") == good)
    check("status names the exe in use", status() == f"Using: {good}")


def test_typing_updates_status_and_app(tmp):
    good = write_stub(tmp, "good", "x")
    missing = os.path.join(tmp, "nope.exe")
    m.app.kiro_cli_path = ""
    open_settings()
    type_path(missing)
    check("typo: stored", m.app.kiro_cli_path == missing)
    check("typo: status says it does not exist", "does not exist" in status())
    type_path(f'  "{good}"  ')
    check("pasted quotes: stored clean", m.app.kiro_cli_path == good)
    check("pasted quotes: status names the exe", status() == f"Using: {good}")


def test_clearing_returns_to_automatic(tmp):
    from_env = write_stub(tmp, "from-env", "x")
    os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = from_env
    open_settings()
    type_path("")
    check("cleared: nothing stored", m.app.kiro_cli_path == "")
    check("cleared: automatic lookup used", status() == f"Using: {from_env}")


def test_setting_is_written_to_the_settings_file(tmp):
    good = write_stub(tmp, "good", "x")
    original = m._SETTINGS_FILE
    m._SETTINGS_FILE = Path(tmp) / "settings.json"
    try:
        open_settings()
        type_path(good)
        saved = json.loads(m._SETTINGS_FILE.read_text())
        check("path saved without pressing Save & Close", saved.get("kiro_cli_path") == good)
    finally:
        m._SETTINGS_FILE = original


def test_generate_uses_the_setting_over_the_environment(tmp):
    repo = Path(tmp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.txt").write_text("hello\n")

    os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = write_stub(
        tmp, "env-copy", "error: Access is denied. (os error 5)", exit_code=1)
    m.app.kiro_cli_path = write_stub(tmp, "setting-copy", "feat(x): from the setting")
    m.app.provider = "kiro"
    m.app.model = "auto"
    m.app.repos["repo"] = SimpleNamespace(path=repo)
    while not m.ui_queue.empty():
        m.ui_queue.get_nowait()
    try:
        m.bg_generate_message("repo")
        kind, _name, msg, err = m.ui_queue.get_nowait()
        check("generate: result posted", kind == "gen_result")
        check("generate: no error", err == "")
        check("generate: reply from the configured exe", msg == "feat(x): from the setting")
    finally:
        m.app.repos.pop("repo", None)


def main():
    dpg.create_context()
    dpg.create_viewport(title="test", width=200, height=200)   # _save_settings reads it
    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    saved_env = os.environ.get(ai_commit_core.KIRO_CLI_EXE_ENV)
    for test in (
        test_field_shows_the_saved_path_and_what_it_resolves_to,
        test_typing_updates_status_and_app,
        test_clearing_returns_to_automatic,
        test_setting_is_written_to_the_settings_file,
        test_generate_uses_the_setting_over_the_environment,
    ):
        print(f"--- {test.__name__}")
        tmp = tempfile.mkdtemp(prefix="kiro-setting-")
        original_file = m._SETTINGS_FILE
        m._SETTINGS_FILE = Path(tmp) / "unused-settings.json"   # never the real one
        try:
            test(tmp)
        except Exception:
            traceback.print_exc()
            check(f"{test.__name__} raised", False)
        finally:
            m._SETTINGS_FILE = original_file
            if saved_env is None:
                os.environ.pop(ai_commit_core.KIRO_CLI_EXE_ENV, None)
            else:
                os.environ[ai_commit_core.KIRO_CLI_EXE_ENV] = saved_env
            shutil.rmtree(tmp, ignore_errors=True)
    dpg.destroy_context()
    print(f"\n{len(_failures)} failure(s)")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
