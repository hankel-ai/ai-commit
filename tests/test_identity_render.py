"""Identity guard UI: queue handlers, popup, Apply Fix, and pins -> git config.

Real Dear PyGui item creation (context only, no viewport). The guard's logic is
covered by tests/test_identity_guard.py against real repos; this file catches
the UI half that logic tests cannot -- a handler that never routes the
message, a popup that drops the fix command or offers Apply Fix for a history
rewrite, pins from the Settings window that never reach git config.

Run: python tests/test_identity_render.py
"""
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"
_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(_ROOT, "ai-commit-gui.py"))

import dearpygui.dearpygui as dpg

import ai_commit_core as core

_failures = []

BLOCK = {"kind": "commit", "level": core.IDENTITY_BLOCK,
         "host": "gitlab.example.com",
         "expected": {"name": "Work", "email": "work@corp.com"},
         "actual": {"name": "Work", "email": "oops@local.com"},
         "fields": ["user.email"],
         "message": "Commit blocked: gitlab.example.com requires Work "
                    "<work@corp.com>, but this repo would commit as Work "
                    "<oops@local.com> (email from .git/config).",
         "fix": "git config --unset user.email",
         "fix_args": [["config", "--unset", "user.email"]], "needs_sync": False}
PUSH_BLOCK = {"kind": "push", "level": core.IDENTITY_BLOCK,
              "host": "gitlab.example.com",
              "expected": {"name": "Work", "email": "work@corp.com"},
              "commits": [{"sha": "0123456789abcdef0123", "parents": ["aa"],
                           "author_name": "P", "author_email": "p@home.com",
                           "committer_name": "P", "committer_email": "p@home.com"}],
              "message": "Push blocked: 1 unpushed commit(s) are not by Work "
                         "<work@corp.com> (required for gitlab.example.com).",
              "fix": "# fix identity first, then:\ngit commit --amend --no-edit --reset-author"}
WARN = dict(BLOCK, level=core.IDENTITY_WARN,
            message="This repo commits as X <x@y> (email from .git/config), "
                    "not your global git identity P <p@home.com>.")


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _fresh():
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


def _fake_repo(path, m):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=m.GenStatus.IDLE, error_message="", commit_message="kept",
        input_tag=None, status_tag=None,
    )


def _drive(m, results):
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1, m)
    q = queue.Queue()
    q.put(("identity_check", r1, results))
    m.ui_queue = q
    shown = []
    m._show_identity_prompt = lambda name, res: shown.append((name, res))
    m.update_repo_status = lambda rs: None
    m.app = m.AppState(repos={r1: rs}, non_git_folders={})
    m.process_queue()
    return rs, shown


def test_handler_block_sets_sticky_error_and_prompts():
    m = _fresh()
    rs, shown = _drive(m, [BLOCK, PUSH_BLOCK])
    check("block_sets_error", rs.gen_status == m.GenStatus.ERROR)
    check("block_error_has_message", "gitlab.example.com requires" in rs.error_message)
    check("block_keeps_commit_message", rs.commit_message == "kept")
    check("block_prompts_once", len(shown) == 1 and len(shown[0][1]) == 2)


def test_handler_warn_leaves_status_and_prompts():
    m = _fresh()
    rs, shown = _drive(m, [WARN])
    check("warn_no_error", rs.gen_status == m.GenStatus.IDLE
          and rs.error_message == "")
    check("warn_prompts", len(shown) == 1)


def _items():
    texts, buttons, windows = [], [], []
    for item in dpg.get_all_items():
        if not dpg.does_item_exist(item):
            continue
        kind = dpg.get_item_type(item)
        if kind == "mvAppItemType::mvText":
            texts.append(str(dpg.get_value(item) or ""))
        elif kind == "mvAppItemType::mvButton":
            buttons.append(dpg.get_item_label(item) or "")
        elif kind == "mvAppItemType::mvWindowAppItem":
            windows.append(dpg.get_item_label(item) or "")
    return "\n".join(texts), buttons, windows


def test_popup_renders_block():
    m = _fresh()
    dpg.create_context()
    try:
        r1 = "C:/repos/proj"
        m.green_btn_theme = m.create_button_theme((50, 130, 75))
        m.app = m.AppState(repos={r1: _fake_repo(r1, m)}, non_git_folders={})
        m._show_identity_prompt(r1, [BLOCK, PUSH_BLOCK])
        blob, buttons, windows = _items()
        check("popup_title_blocked", any("Blocked" in w and "proj" in w for w in windows))
        check("popup_shows_message", "requires Work <work@corp.com>" in blob)
        check("popup_lists_commit", "01234567" in blob and "p@home.com" in blob)
        check("popup_shows_fixes", "git config --unset user.email" in blob
              and "--reset-author" in blob)
        check("popup_buttons", "Copy Fix" in buttons and "Close" in buttons)
        check("popup_apply_fix_for_config_fix", "Apply Fix" in buttons)
    finally:
        dpg.destroy_context()


def test_popup_push_only_has_no_apply_fix():
    """A history rewrite is never run for the user: copy only."""
    m = _fresh()
    dpg.create_context()
    try:
        r1 = "C:/repos/proj"
        m.green_btn_theme = m.create_button_theme((50, 130, 75))
        m.app = m.AppState(repos={r1: _fake_repo(r1, m)}, non_git_folders={})
        m._show_identity_prompt(r1, [PUSH_BLOCK])
        _blob, buttons, _w = _items()
        check("push_only_no_apply", "Apply Fix" not in buttons and "Copy Fix" in buttons)
    finally:
        dpg.destroy_context()


def test_popup_renders_warn():
    m = _fresh()
    dpg.create_context()
    try:
        r1 = "C:/repos/proj"
        m.app = m.AppState(repos={r1: _fake_repo(r1, m)}, non_git_folders={})
        m._show_identity_prompt(r1, [WARN])
        blob, _buttons, windows = _items()
        check("warn_title", any("Warning" in w for w in windows))
        check("warn_says_went_ahead", "went ahead" in blob)
    finally:
        dpg.destroy_context()


def test_handler_identity_fixed_clears_block():
    m = _fresh()
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1, m)
    rs.gen_status, rs.error_message = m.GenStatus.ERROR, "Commit blocked: x"
    q = queue.Queue()
    q.put(("identity_fixed", r1, []))
    m.ui_queue = q
    m.update_repo_status = lambda rs: None
    m.app = m.AppState(repos={r1: rs}, non_git_folders={})
    m.process_queue()
    check("fixed_clears_error", rs.error_message == ""
          and rs.gen_status == m.GenStatus.DONE)
    check("fixed_keeps_message", rs.commit_message == "kept")


class _GitSandbox:
    """Temp global git config + managed hub dir; restores everything on close."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="ident-render-"))
        cfg = self.root / "global.cfg"
        cfg.write_text("[user]\n\tname = Personal Name\n\temail = personal@home.com\n")
        self._env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self._hub = core.IDENTITY_HUB_PATH
        core.IDENTITY_HUB_PATH = self.root / "managed" / "hub.gitconfig"

    def repo(self, url):
        path = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=path, check=True)
        return path

    def email(self, path):
        return subprocess.run(["git", "config", "user.email"], cwd=path,
                              capture_output=True, text=True).stdout.strip()

    def close(self):
        core.IDENTITY_HUB_PATH = self._hub
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)


def _wait_for(q, kind, timeout=30):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if msg[0] == kind:
            return msg
    return None


def test_settings_pins_are_written_to_git_config():
    """Pins from the Settings window reach git without any hand editing."""
    sb = _GitSandbox()
    m = _fresh()
    try:
        m._save_settings = lambda: None  # needs a viewport; not under test
        m.app = m.AppState()
        m.ui_queue = queue.Queue()
        m._handle_settings_msg({"type": "set", "key": "identity_pins", "value": [
            {"host": " GitLab.Example.com ", "name": "Work", "email": "work@corp.com",
             "login": "jdoe"}, {"host": "", "name": "half", "email": "", "login": ""}]})
        check("pins_normalized", m.app.identity_pins == [
            {"host": "gitlab.example.com", "name": "Work", "email": "work@corp.com",
             "login": "jdoe"}])
        msg = _wait_for(m.ui_queue, "identity_sync")
        check("sync_posted", msg is not None and msg[1]["ok"])
        m.ui_queue.put(msg)
        m.process_queue()
        check("status_applied", m.app.identity_status["ok"]
              and "applied" in " ".join(m.app.identity_status["lines"]))
        repo = sb.repo("https://gitlab.example.com/g/p.git")
        check("git_resolves_pin", sb.email(repo) == "work@corp.com")
    finally:
        m.executor.shutdown(wait=True)
        sb.close()


def test_apply_fix_button_path_fixes_real_repo():
    sb = _GitSandbox()
    m = _fresh()
    try:
        pins = [{"host": "gitlab.example.com", "name": "Work", "email": "work@corp.com",
                 "login": ""}]
        core.sync_identity_config(pins)
        repo = sb.repo("https://gitlab.example.com/g/p.git")
        subprocess.run(["git", "config", "user.email", "oops@local.com"], cwd=repo, check=True)
        result = core.check_commit_identity(str(repo), pins)
        check("precondition_blocked", result and result["level"] == core.IDENTITY_BLOCK)
        m.app = m.AppState(repos={str(repo): _fake_repo(repo, m)}, non_git_folders={},
                           identity_pins=pins)
        m.ui_queue = queue.Queue()
        m.bg_apply_identity_fix(str(repo), [result])
        msg = m.ui_queue.get_nowait()
        check("fix_reports_clean", msg[0] == "identity_fixed" and msg[2] == [])
        check("fix_removed_override", sb.email(repo) == "work@corp.com")
    finally:
        sb.close()


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                check(f"{name} raised {type(exc).__name__}: {exc}", False)
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
