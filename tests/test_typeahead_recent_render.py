"""Type-to-filter searches every repo, even while "Recent only" is on.

"Recent only" skips building idle rows altogether, so the plain type-to-filter
(which only hides/shows rows that exist) could never find them. A non-empty
buffer now lifts the recency filter: rebuild_repos_ui builds every row and the
typed text narrows them. Clearing the buffer drops the idle rows again.

Drives the real rebuild_repos_ui and the real key handler inside a Dear PyGui
context (no viewport), then reads which rows exist and which are shown.

Also covers the Copy-Path link button (row order Copy-Path, Folder, Terminal) on repo and non-git rows.

Run: python tests/test_typeahead_recent_render.py
"""
import importlib.util
import os
import sys
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
ACTIVE = str(Path("C:/repos/ai-commit"))
IDLE = str(Path("C:/repos/old-thing"))
IDLE_DIR = str(Path("C:/repos/old-notes"))


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _result(key, ts):
    return {"path": Path(key), "entries": [],
            "remote_url": "https://gitlab.example.com/team/ai-commit.git" if key == ACTIVE else "", "branch": "main",
            "last_commit_ts": ts, "last_commit_date": "", "ahead": 0, "behind": 0}


def _setup():
    dpg.create_context()
    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    m.orange_btn_theme = m.create_button_theme((200, 130, 30))
    m.pull_btn_theme = m.create_button_theme((200, 60, 60))
    with dpg.theme() as link_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
    m.link_btn_theme = link_theme
    for alias in ("force_pause_header_theme", "force_active_header_theme",
                  "public_header_theme"):
        with dpg.theme() as t:
            pass
        dpg.add_alias(alias, t)
    m.app = SimpleNamespace(
        repos={}, non_git_folders={}, active_gh_account="hankel-ai",
        expand_on_next_build=set(), collapse_on_next_build=set(),
        repo_overrides={}, paused=False, expanded_changes=set(),
        typeahead_buf="", typeahead_miss=False,
        recent_only=True, recent_days=14, sort_by_date=False,
        show_non_git_folders=True, auto_generate=False, poll_pending=set(),
        last_results={ACTIVE: _result(ACTIVE, NOW), IDLE: _result(IDLE, OLD)},
        last_non_git={IDLE_DIR: {"path": Path(IDLE_DIR), "name": "old-notes",
                                 "mtime": OLD}},
    )
    with dpg.window(tag="primary"):
        dpg.add_text("", tag="typeahead_label")
        dpg.add_text("", tag="hidden_count_label")
        with dpg.child_window(tag="repos_container"):
            pass
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)


def _shown():
    """Names of rows that exist in the list AND are shown."""
    children = set(dpg.get_item_children("repos_container", 1) or [])
    rows = [(rs.header_tag, rs.name) for rs in m.app.repos.values()]
    rows += [(n.header_tag, n.name) for n in m.app.non_git_folders.values()]
    return sorted(name for tag, name in rows
                  if tag in children
                  and dpg.get_item_configuration(tag)["show"])


def _press(*keys):
    for k in keys:
        m.cb_typeahead_key(None, k, None)


def _type(text):
    _press(*[getattr(dpg, "mvKey_Minus") if c == "-" else
             getattr(dpg, "mvKey_" + c.upper()) for c in text])


def test_recent_hides_idle_initially():
    check("recent_only_shows_active_only", _shown() == ["ai-commit"])
    check("hidden_count_shown", dpg.get_value("hidden_count_label") == "2 hidden")


def test_typing_finds_idle_repo():
    _type("old-t")
    check("typing_finds_idle_repo", _shown() == ["old-thing"])
    check("hidden_count_cleared_while_searching",
          dpg.get_value("hidden_count_label") == "")
    check("checkbox_state_untouched", m.app.recent_only is True)


def test_typing_finds_idle_non_git_folder():
    _press(dpg.mvKey_Escape)
    _type("notes")
    check("typing_finds_idle_folder", _shown() == ["old-notes"])


def test_broad_match_spans_all():
    _press(dpg.mvKey_Escape)
    _type("o")
    check("broad_match_includes_idle_and_active",
          _shown() == ["ai-commit", "old-notes", "old-thing"])


def test_backspace_to_empty_restores_recent_filter():
    _press(dpg.mvKey_Back)
    check("empty_buffer_reapplies_recent", _shown() == ["ai-commit"])
    check("hidden_count_back", dpg.get_value("hidden_count_label") == "2 hidden")


def test_escape_restores_recent_filter():
    _type("old")
    check("old_before_escape", _shown() == ["old-notes", "old-thing"])
    _press(dpg.mvKey_Escape)
    check("escape_reapplies_recent", _shown() == ["ai-commit"])


def test_poll_rebuild_keeps_search_override():
    _type("thing")
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("poll_rebuild_keeps_idle_match", _shown() == ["old-thing"])
    _press(dpg.mvKey_Escape)


def test_recent_off_unchanged():
    m.app.recent_only = False
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    check("recent_off_shows_all",
          _shown() == ["ai-commit", "old-notes", "old-thing"])
    _type("ai")
    check("recent_off_filter_still_works", _shown() == ["ai-commit"])
    _press(dpg.mvKey_Escape)
    m.app.recent_only = True
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)


def _buttons(header_tag):
    out = []

    def walk(tag):
        for child_list in dpg.get_item_children(tag).values():
            for c in child_list:
                if dpg.get_item_type(c) == "mvAppItemType::mvButton":
                    out.append(c)
                walk(c)

    walk(header_tag)
    return out


def test_copy_path_button():
    m.app.recent_only = False
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    for key, header in ((ACTIVE, m.app.repos[ACTIVE].header_tag),
                        (IDLE_DIR, m.app.non_git_folders[IDLE_DIR].header_tag)):
        btns = _buttons(header)
        labels = [dpg.get_item_label(b) for b in btns]
        kind = "repo" if key == ACTIVE else "folder"
        check(f"link_order_{kind}",
              labels[:3] == ["Copy-Path", "Folder", "Terminal"])
        if kind == "repo":
            check("remote_link_named_git_remote",
                  "Git-Remote" in labels and "GitHub" not in labels)
        btn = btns[labels.index("Copy-Path")]
        cfg = dpg.get_item_configuration(btn)
        check(f"copypath_wired_{kind}",
              cfg["callback"] is m.cb_copy_path and cfg["user_data"] == key)
    m.app.recent_only = True


def main():
    _setup()
    test_recent_hides_idle_initially()
    test_typing_finds_idle_repo()
    test_typing_finds_idle_non_git_folder()
    test_broad_match_spans_all()
    test_backspace_to_empty_restores_recent_filter()
    test_escape_restores_recent_filter()
    test_poll_rebuild_keeps_search_override()
    test_recent_off_unchanged()
    test_copy_path_button()
    dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
