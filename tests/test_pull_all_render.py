"""Bulk Pull: eligibility rules and the dialog those rules produce.

`pull_all_eligible` is pure and asserted directly. The dialog is then built
with a REAL Dear PyGui context (no viewport) and its item tree walked for the
rendered text and buttons -- the predicate test alone cannot catch a prompt
that offers the wrong repos, renders no confirm button, or shows the confirm
dialog when there is nothing to pull.

Regressions this guards:
  - pulling repos that are clean but already up to date (pointless network,
    and the crash-prone status churn that came with it)
  - pulling repos with pending local changes (conflicted merge)
  - the "nothing to pull" case rendering a dead confirm dialog

Run: python tests/test_pull_all_render.py
"""
import importlib.util
import os
import sys
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


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def repo(entries=(), behind=0):
    return SimpleNamespace(entries=list(entries), behind=behind)


# --------------------------------------------------------------------------
# Pure eligibility rules
# --------------------------------------------------------------------------

def test_eligibility():
    repos = {
        "clean_behind": repo(behind=2),
        "clean_synced": repo(behind=0),
        "dirty_behind": repo(entries=[("M", "a.txt")], behind=5),
        "dirty_synced": repo(entries=[("M", "b.txt")], behind=0),
    }
    eligible, dirty = m.pull_all_eligible(repos)
    check("only_clean_and_behind_is_eligible", eligible == ["clean_behind"])
    check("dirty_counted_regardless_of_behind", dirty == 2)

    eligible, dirty = m.pull_all_eligible({})
    check("no_repos_no_eligible", eligible == [] and dirty == 0)

    eligible, dirty = m.pull_all_eligible({"a": repo(behind=0), "b": repo(behind=0)})
    check("all_synced_none_eligible", eligible == [] and dirty == 0)

    eligible, dirty = m.pull_all_eligible({"a": repo(entries=[("M", "x")], behind=1)})
    check("all_dirty_none_eligible", eligible == [] and dirty == 1)

    eligible, _ = m.pull_all_eligible({"a": repo(behind=1), "b": repo(behind=9)})
    check("all_clean_behind_all_eligible", sorted(eligible) == ["a", "b"])


# --------------------------------------------------------------------------
# Rendered dialog
# --------------------------------------------------------------------------

def _setup_context():
    """dpg context plus the themes the prompt binds to.

    green_btn_theme is normally assigned in main() after create_viewport; the
    prompt binds it unconditionally, so it must exist here too.
    """
    dpg.create_context()
    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    m.app = SimpleNamespace(repos={})


def _render(repos):
    """Build the prompt for *repos*; return (joined_text, button_labels)."""
    m.app.repos = repos
    win = m._show_pull_all_prompt()
    texts, buttons = [], []

    def walk(tag):
        item_type = dpg.get_item_type(tag)
        if item_type == "mvAppItemType::mvText":
            texts.append(dpg.get_value(tag) or "")
        elif item_type == "mvAppItemType::mvButton":
            buttons.append(dpg.get_item_label(tag))
        for child_list in dpg.get_item_children(tag).values():
            for c in child_list:
                walk(c)

    walk(win)
    dpg.delete_item(win)
    return " ".join(texts), buttons


def test_confirm_dialog():
    text, buttons = _render({
        "clean_behind1": repo(behind=1),
        "clean_behind2": repo(behind=3),
        "clean_synced": repo(behind=0),
        "dirty": repo(entries=[("M", "a.txt")], behind=4),
    })
    check("confirm_counts_only_eligible", "Pull 2 of 4" in text)
    check("confirm_reports_skipped_dirty", "1 repo(s) with pending" in text)
    check("confirm_has_pull_all_button", "Pull All" in buttons)
    check("confirm_has_cancel_button", "Cancel" in buttons)

    # A synced-but-clean repo is skipped without being called dirty.
    text, _ = _render({"a": repo(behind=1), "b": repo(behind=0)})
    check("synced_repo_not_reported_as_dirty", "pending" not in text)


def test_notice_when_nothing_to_pull():
    text, buttons = _render({})
    check("no_repos_notice", "No repos are being watched." in text)
    check("no_repos_has_no_confirm", "Pull All" not in buttons and "OK" in buttons)

    text, buttons = _render({"a": repo(entries=[("M", "x")], behind=2)})
    check("all_dirty_notice", "pending local changes" in text)
    check("all_dirty_has_no_confirm", "Pull All" not in buttons)

    text, buttons = _render({"a": repo(behind=0), "b": repo(behind=0)})
    check("all_synced_notice", "already up to date" in text)
    check("all_synced_has_no_confirm", "Pull All" not in buttons)


def main():
    test_eligibility()
    try:
        _setup_context()
    except Exception as exc:  # no GPU/display available
        print(f"SKIP: cannot create a Dear PyGui context here ({exc})")
    else:
        try:
            test_confirm_dialog()
            test_notice_when_nothing_to_pull()
        finally:
            dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
