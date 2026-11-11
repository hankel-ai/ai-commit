"""Pinned repos: always shown, always first, amber header, Pin/Unpin menu.

Drives the real rebuild_repos_ui, the real right-click menu builder and the
real Pin/Unpin callback inside a Dear PyGui context (no viewport), then reads
which rows exist, their order, their bound theme and their label. Also checks
that pinning never feeds the poll schedule (bg_poll_repos ignores it) and that
the real _save_settings writes the pin list.

Run: python tests/test_pin_render.py
"""
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

import dearpygui.dearpygui as dpg

_failures = []

NOW = time.time()
OLD = NOW - 365 * 86400
ACTIVE_A = str(Path("C:/repos/alpha"))
ACTIVE_B = str(Path("C:/repos/bravo"))
IDLE_Z = str(Path("C:/repos/zulu"))      # idle + alphabetically last


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _result(key, ts):
    return {"path": Path(key), "entries": [], "remote_url": "", "branch": "main",
            "last_commit_ts": ts, "last_commit_date": "", "ahead": 0, "behind": 0}


THEMES = ("force_pause_header_theme", "force_active_header_theme",
          "public_header_theme", "pinned_header_theme")


def _setup():
    dpg.create_context()
    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    m.orange_btn_theme = m.create_button_theme((200, 130, 30))
    m.pull_btn_theme = m.create_button_theme((200, 60, 60))
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
    m.link_btn_theme = link_theme
    for alias in THEMES:
        with dpg.theme() as t:
            pass
        dpg.add_alias(alias, t)
    m.app = SimpleNamespace(
        repos={}, non_git_folders={}, active_gh_account="hankel-ai",
        expand_on_next_build=set(), collapse_on_next_build=set(),
        repo_overrides={}, pinned_repos=[], paused=False, expanded_changes=set(),
        typeahead_buf="", typeahead_miss=False,
        recent_only=True, recent_days=14, sort_by_date=False,
        show_non_git_folders=True, auto_generate=False, poll_pending=set(),
        last_results={ACTIVE_A: _result(ACTIVE_A, NOW - 60),
                      ACTIVE_B: _result(ACTIVE_B, NOW),
                      IDLE_Z: _result(IDLE_Z, OLD)},
        last_non_git={},
    )
    m._save_settings = lambda: None  # needs a viewport; round trip tested below
    with dpg.window(tag="primary"):
        dpg.add_text("", tag="typeahead_label")
        dpg.add_text("", tag="hidden_count_label")
        with dpg.child_window(tag="repos_container"):
            pass
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)


def _order():
    """Names of built rows in on-screen order."""
    by_tag = {rs.header_tag: rs.name for rs in m.app.repos.values()}
    return [by_tag[c] for c in (dpg.get_item_children("repos_container", 1) or [])
            if c in by_tag]


def _theme_alias(key):
    # dpg returns the alias string when the theme was bound by alias.
    t = dpg.get_item_theme(m.app.repos[key].header_tag)
    for alias in THEMES:
        if t in (alias, dpg.get_alias_id(alias)):
            return alias
    return None


def _menu_buttons(key):
    m.cb_repo_right_click(None, None, key)
    btns = [c for c in dpg.get_item_children("repo_context_menu", 1)
            if dpg.get_item_type(c) == "mvAppItemType::mvButton"]
    return {dpg.get_item_label(b): b for b in btns}


def _click(btn):
    cfg = dpg.get_item_configuration(btn)
    cfg["callback"](btn, None, cfg["user_data"])


def test_baseline():
    check("idle_hidden_unpinned", _order() == ["alpha", "bravo"])
    check("menu_offers_pin", "Pin" in _menu_buttons(IDLE_Z))


def test_pin_idle_repo():
    _click(_menu_buttons(IDLE_Z)["Pin"])
    check("pin_recorded", m.app.pinned_repos == [IDLE_Z])
    check("menu_closed_after_pin", not dpg.does_item_exist("repo_context_menu"))
    check("pinned_idle_shown_first", _order() == ["zulu", "alpha", "bravo"])
    check("pinned_theme_bound", _theme_alias(IDLE_Z) == "pinned_header_theme")
    check("unpinned_no_pin_theme", _theme_alias(ACTIVE_A) is None)
    check("hidden_count_excludes_pinned",
          dpg.get_value("hidden_count_label") == "")
    check("menu_offers_unpin", "Unpin" in _menu_buttons(IDLE_Z)
          and "Pin" not in _menu_buttons(IDLE_Z))
    dpg.delete_item("repo_context_menu")


def test_pinned_first_in_date_sort():
    m.app.sort_by_date = True
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("date_sort_pinned_first", _order() == ["zulu", "bravo", "alpha"])
    m.app.sort_by_date = False
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)


def test_multiple_pins_keep_sort_among_themselves():
    _click(_menu_buttons(ACTIVE_B)["Pin"])
    check("two_pins_alpha_order", _order() == ["bravo", "zulu", "alpha"])


def test_pin_plus_override():
    m.app.repo_overrides[IDLE_Z] = "pause"
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("pin_color_wins_over_pause", _theme_alias(IDLE_Z) == "pinned_header_theme")
    label = dpg.get_item_label(m.app.repos[IDLE_Z].header_tag)
    check("pause_named_in_label", "FORCE PAUSED" in label)
    m.app.repo_overrides[IDLE_Z] = "active"
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("active_named_in_label",
          "FORCE ACTIVE" in dpg.get_item_label(m.app.repos[IDLE_Z].header_tag))
    m.app.repo_overrides.clear()
    # An unpinned forced repo keeps its old color and no label tag.
    m.app.repo_overrides[ACTIVE_A] = "active"
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("unpinned_override_theme_unchanged",
          _theme_alias(ACTIVE_A) == "force_active_header_theme")
    check("unpinned_override_no_label_tag",
          "FORCE" not in dpg.get_item_label(m.app.repos[ACTIVE_A].header_tag))
    m.app.repo_overrides.clear()
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)


def test_unpin_restores():
    _click(_menu_buttons(IDLE_Z)["Unpin"])
    _click(_menu_buttons(ACTIVE_B)["Unpin"])
    check("unpinned_list_empty", m.app.pinned_repos == [])
    check("unpin_hides_idle_again", _order() == ["alpha", "bravo"])
    check("hidden_count_back", dpg.get_value("hidden_count_label") == "1 hidden")
    check("unpin_override_untouched", m.app.repo_overrides == {})


def test_poll_schedule_ignores_pins():
    src = inspect.getsource(m.bg_poll_repos)
    check("bg_poll_repos_never_reads_pins",
          "pinned" not in src and "_is_pinned" not in src)


def test_settings_round_trip():
    real_app, m.app = m.app, m.AppState()
    m.app.pinned_repos = [IDLE_Z]
    real_file = m._SETTINGS_FILE
    tmp = Path(tempfile.mkdtemp()) / "settings.json"
    m._SETTINGS_FILE = tmp
    orig = (dpg.get_viewport_pos, dpg.get_viewport_width, dpg.get_viewport_height)
    dpg.get_viewport_pos = lambda: (0, 0)
    dpg.get_viewport_width = lambda: 800
    dpg.get_viewport_height = lambda: 600
    try:
        m._save_settings = SAVE_SETTINGS
        m._save_settings()
        saved = json.loads(tmp.read_text())
        check("pins_persisted", saved.get("pinned_repos") == [IDLE_Z])
    finally:
        dpg.get_viewport_pos, dpg.get_viewport_width, dpg.get_viewport_height = orig
        m._SETTINGS_FILE = real_file
        m.app = real_app


SAVE_SETTINGS = m._save_settings


def main():
    _setup()
    test_baseline()
    test_pin_idle_repo()
    test_pinned_first_in_date_sort()
    test_multiple_pins_keep_sort_among_themselves()
    test_pin_plus_override()
    test_unpin_restores()
    test_poll_schedule_ignores_pins()
    test_settings_round_trip()
    dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
