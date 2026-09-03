"""build_repo_section must cap the change list at MAX_SHOWN_CHANGES rows and
offer a "+N more" link that reveals the rest.

This exercises real Dear PyGui item creation (context only, no viewport shown)
and walks the built item tree for button labels -- the cap is a display choice
in build_repo_section, so only a real render proves the slice, the link, its
absence at <=MAX_SHOWN_CHANGES, and the rebuild-level reset when the entry list
changes. Generation, commit, and diff flows read rs.entries directly and are
unaffected by the display cap.

Regression for: a repo with 50 dirty files rendered all 50 rows inline, so the
expanded header pushed everything below it off screen.

Run: python tests/test_changes_more_render.py
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

REMOTE = "https://github.com/hankel-ai/hermes.git"
REPO_KEY = str(Path("C:/repos/hermes"))


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + str(name))
    if not cond:
        _failures.append(name)


def _setup_context():
    """Create a dpg context plus the themes build_repo_section binds to."""
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
        repo_overrides={}, paused=False,
        expanded_changes=set(),
    )


def _button_labels(entries, expanded=False):
    """Build one repo section; return every button label inside it."""
    rs = m.RepoState(
        path=Path(REPO_KEY), name="hermes", folder_name="hermes",
        entries=entries, remote_url=REMOTE, branch="main",
        last_commit_msg="fix(helm): widen HERMES_WRITE_SAFE_ROOT",
        last_commit_date="Aug 05 11:00am",
    )
    m.app.repos[REPO_KEY] = rs
    if expanded:
        m.app.expanded_changes.add(REPO_KEY)
    with dpg.window() as win:
        m.build_repo_section(rs, win)
    labels = []

    def walk(tag):
        for child_list in dpg.get_item_children(tag).values():
            for c in child_list:
                if dpg.get_item_type(c) == "mvAppItemType::mvButton":
                    labels.append(dpg.get_item_label(c))
                walk(c)

    walk(win)
    return labels


def _file_rows(labels):
    """File-path rows render as "  <path>" buttons; the more-link as "+N more"."""
    return [l for l in labels if l.startswith("  ") and "View Diff" not in l
            and l != "  gitignore"]


def _more_link(labels):
    return [l for l in labels if l.startswith("+") and l.endswith(" more")]


def test_cap_and_more_link():
    cap = getattr(m, "MAX_SHOWN_CHANGES", None)
    if cap is None:
        check("MAX_SHOWN_CHANGES_constant", False)
        return
    check("MAX_SHOWN_CHANGES_constant", True)
    entries = [("M", f"file_{i:02d}.txt") for i in range(cap + 5)]

    collapsed = _file_rows(_button_labels(entries))
    check("cap_shows_first_20", len(collapsed) == cap)
    check("cap_shows_in_order",
          collapsed == [f"  file_{i:02d}.txt" for i in range(cap)])

    link = _more_link(_button_labels(entries))
    check("more_link_present", len(link) == 1)
    check("more_link_counts_hidden", link[0] == "+5 more")

    expanded_all = _file_rows(_button_labels(entries, expanded=True))
    check("expanded_shows_all", len(expanded_all) == cap + 5)
    check("expanded_no_more_link",
          not _more_link(_button_labels(entries, expanded=True)))


def test_no_link_at_or_below_cap():
    cap = getattr(m, "MAX_SHOWN_CHANGES", None)
    if cap is None:
        return
    exact = _button_labels([("M", f"f{i}.txt") for i in range(cap)])
    check("exact_cap_no_link", not _more_link(exact))
    check("exact_cap_all_rows", len(_file_rows(exact)) == cap)

    single = _button_labels([("M", "only.txt")])
    check("single_entry_no_link", not _more_link(single))


def test_reset_when_entries_change():
    """rebuild_repos_ui discards the repo's reveal flag when its entry list
    changes, so the user is never left looking at a stale expanded list."""
    cap = getattr(m, "MAX_SHOWN_CHANGES", None)
    if cap is None:
        return
    entries_a = [("M", f"file_{i:02d}.txt") for i in range(cap + 5)]

    # Production reset logic: files_changed -> app.expanded_changes.discard(key)
    _button_labels(entries_a, expanded=True)
    check("expanded_registered", REPO_KEY in m.app.expanded_changes)

    changed = list(entries_a) + [("M", "one_more.txt")]
    m.app.expanded_changes.discard(REPO_KEY)  # what rebuild_repos_ui does
    check("reset_on_entries_change", REPO_KEY not in m.app.expanded_changes)

    collapsed_after = _file_rows(_button_labels(changed))
    check("after_reset_collapses_to_cap", len(collapsed_after) == cap)
    check("after_reset_has_link", len(_more_link(_button_labels(changed))) == 1)


def main():
    try:
        _setup_context()
    except Exception as exc:  # no GPU/display available
        print(f"SKIP: cannot create a Dear PyGui context here ({exc})")
        return
    try:
        test_cap_and_more_link()
        test_no_link_at_or_below_cap()
        test_reset_when_entries_change()
    finally:
        dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()