"""Type-to-filter on the real repo list: typed text hides every row whose name
does not contain it, and Backspace/Escape bring the rows back.

Builds real repo sections with build_repo_section inside a real Dear PyGui
context (no viewport) and drives the global key handler with real dpg key
codes, then reads each header's `show` flag -- the pure matcher test cannot
catch a handler that matches correctly but never hides anything.

Run: python tests/test_typeahead_render.py
"""
import importlib.util
import os
import sys
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

NAMES = ["ai-commit", "ai-news", "hermes", "media-stack", "Media-Vault"]


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


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
    )
    with dpg.window(tag="primary"):
        dpg.add_text("", tag="typeahead_label")
        with dpg.child_window(tag="repos_container"):
            pass
    for name in NAMES:
        key = str(Path("C:/repos") / name)
        rs = m.RepoState(path=Path(key), name=name, folder_name=name,
                         entries=[], branch="main")
        m.app.repos[key] = rs
        m.build_repo_section(rs, "repos_container")
    # A streaming placeholder, as rebuild_repos_ui adds for pending repos.
    dpg.add_text("  pending-repo  ...", tag="placeholder",
                 parent="repos_container")


def _shown():
    """Names of the repo rows currently shown."""
    return [rs.name for rs in m.app.repos.values()
            if dpg.get_item_configuration(rs.header_tag)["show"]]


def _press(*keys):
    for k in keys:
        m.cb_typeahead_key(None, k, None)


def _type(text):
    _press(*[getattr(dpg, "mvKey_Minus") if c == "-" else
             getattr(dpg, "mvKey_" + c.upper()) for c in text])


def test_filter_hides_non_matching():
    _type("ai")
    check("ai_shows_both_ai_repos", _shown() == ["ai-commit", "ai-news"])
    check("buffer_echoed", dpg.get_value("typeahead_label") == "> ai")
    check("placeholder_hidden_while_filtering",
          not dpg.get_item_configuration("placeholder")["show"])


def test_contains_mid_word():
    _press(dpg.mvKey_Escape)
    _type("stack")
    check("stack_mid_name", _shown() == ["media-stack"])


def test_case_insensitive_contains():
    _press(dpg.mvKey_Escape)
    _type("media")
    check("media_both_cases", _shown() == ["media-stack", "Media-Vault"])


def test_backspace_widens():
    _press(dpg.mvKey_Escape)
    _type("ai-n")
    check("ai_n_narrows", _shown() == ["ai-news"])
    _press(dpg.mvKey_Back, dpg.mvKey_Back)
    check("backspace_widens", _shown() == ["ai-commit", "ai-news"])
    _press(dpg.mvKey_Back, dpg.mvKey_Back)
    check("backspace_to_empty_shows_all", _shown() == NAMES)
    check("placeholder_back", dpg.get_item_configuration("placeholder")["show"])
    check("label_cleared", dpg.get_value("typeahead_label") == "")


def test_no_match_hides_all_and_flags():
    _press(dpg.mvKey_Escape)
    _type("zzz")
    check("no_match_hides_all", _shown() == [])
    check("no_match_flagged", m.app.typeahead_miss
          and "(no match)" in dpg.get_value("typeahead_label"))


def test_escape_restores_everything():
    _press(dpg.mvKey_Escape)
    check("escape_shows_all", _shown() == NAMES)
    check("escape_clears_buffer", m.app.typeahead_buf == "")
    check("escape_clears_miss", not m.app.typeahead_miss)


def test_filter_survives_rebuilt_rows():
    """A poll rebuild re-creates rows (all shown); re-applying must re-hide."""
    _type("herm")
    dpg.delete_item("repos_container", children_only=True)
    for rs in m.app.repos.values():
        m.build_repo_section(rs, "repos_container")
    check("rebuilt_rows_start_shown", len(_shown()) == len(NAMES))
    m._typeahead_apply(scroll_top=False)
    check("reapplied_after_rebuild", _shown() == ["hermes"])
    _press(dpg.mvKey_Escape)


def main():
    _setup()
    test_filter_hides_non_matching()
    test_contains_mid_word()
    test_case_insensitive_contains()
    test_backspace_widens()
    test_no_match_hides_all_and_flags()
    test_escape_restores_everything()
    test_filter_survives_rebuilt_rows()
    dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
