"""build_repo_section must render a Push button when a commit is unpushed.

This exercises real Dear PyGui item creation (context only, no viewport shown)
and walks the built item tree for button labels -- the pure predicate test in
test_push_only.py cannot catch a button that was never wired into the section.

Regression for: a clean tree with ahead > 0 (commit landed, push failed)
rendered only 'Terminal, Folder, Clean, GitHub, More' -- no way to retry the
push, and a manual Refresh just rebuilt the same dead end.

Run: python tests/test_push_button_render.py
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


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _setup_context():
    """Create a dpg context plus the themes build_repo_section binds to.

    Those globals are normally assigned in main() after create_viewport; the
    section binds them unconditionally, so they must exist here too.
    """
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
    )


def _button_labels(entries, ahead, behind, remote=REMOTE):
    """Build one repo section and return every button label inside it."""
    rs = m.RepoState(
        path=Path("C:/repos/hermes"), name="hermes", folder_name="hermes",
        entries=entries, remote_url=remote, branch="main",
        ahead=ahead, behind=behind,
        last_commit_msg="fix(helm): widen HERMES_WRITE_SAFE_ROOT",
        last_commit_date="Aug 05 11:00am",
    )
    m.app.repos[str(rs.path)] = rs
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


def test_push_button_visibility():
    # The reported bug: commit landed, push failed, tree is clean.
    clean_ahead = _button_labels([], ahead=1, behind=0)
    check("clean_tree_ahead_offers_push", "Push" in clean_ahead)

    # Behind: a plain push would be rejected non-fast-forward.
    behind = _button_labels([], ahead=0, behind=2)
    check("behind_offers_pull_not_push",
          "Preview Pull" in behind and "Push" not in behind)

    both = _button_labels([], ahead=1, behind=2)
    check("ahead_and_behind_offers_pull_not_push",
          "Preview Pull" in both and "Push" not in both)

    synced = _button_labels([], ahead=0, behind=0)
    check("synced_offers_neither",
          "Push" not in synced and "Preview Pull" not in synced)

    # Push coexists with the commit flow rather than replacing it.
    dirty = _button_labels([("M", "values.yaml")], ahead=1, behind=0)
    check("dirty_and_ahead_offers_both",
          "Push" in dirty and "Commit & Push" in dirty)

    no_remote = _button_labels([], ahead=1, behind=0, remote="")
    check("no_remote_offers_no_push", "Push" not in no_remote)


def main():
    try:
        _setup_context()
    except Exception as exc:  # no GPU/display available
        print(f"SKIP: cannot create a Dear PyGui context here ({exc})")
        return
    try:
        test_push_button_visibility()
    finally:
        dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
