"""The stash-on-blocked-pull dialogs, rendered with a real Dear PyGui context.

`ai_commit_core` decides *whether* to offer the stash; these tests cover what
the user is actually shown when it does -- the blocked paths, the repo names in
the bulk variant, and the presence of both buttons. A dialog that renders no
confirm button, or lists the wrong repos, passes every pure test and is still
broken.

No viewport is created, so this runs headless like test_pull_all_render.py.

Run: python tests/test_pull_stash_render.py
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


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def _setup_context():
    dpg.create_context()
    m.green_btn_theme = m.create_button_theme((50, 130, 75))
    m.app = SimpleNamespace(repos={})


def _walk(win):
    texts, buttons = [], []

    def go(tag):
        item_type = dpg.get_item_type(tag)
        if item_type == "mvAppItemType::mvText":
            texts.append(dpg.get_value(tag) or "")
        elif item_type == "mvAppItemType::mvButton":
            buttons.append(dpg.get_item_label(tag))
        for child_list in dpg.get_item_children(tag).values():
            for c in child_list:
                go(c)

    go(win)
    dpg.delete_item(win)
    return " ".join(texts), buttons


def _repo(name):
    return SimpleNamespace(name=name, path=Path(f"C:/repos/{name}"), entries=[])


# --------------------------------------------------------------------------
# Single-repo dialog
# --------------------------------------------------------------------------

def test_single_dialog_lists_blocked_paths():
    m.app.repos = {"r1": _repo("dctm-server-image")}
    text, buttons = _walk(m._show_pull_stash_prompt("r1", ["Dockerfile"]))
    check("single_counts_paths", "1 local change(s)" in text)
    check("single_names_path", "Dockerfile" in text)
    check("single_explains_untracked", "including untracked files" in text)
    check("single_warns_conflict_keeps_stash", "stash is kept" in text)
    check("single_has_confirm", "Stash, pull & restore" in buttons)
    check("single_has_cancel", "Cancel" in buttons)


def test_single_dialog_truncates_long_lists():
    m.app.repos = {"r1": _repo("big")}
    paths = [f"file{i}.txt" for i in range(12)]
    text, _ = _walk(m._show_pull_stash_prompt("r1", paths))
    check("single_counts_all", "12 local change(s)" in text)
    check("single_shows_first_eight", "file7.txt" in text)
    check("single_truncates", "+4 more" in text)
    check("single_hides_ninth", "file8.txt" not in text)


# --------------------------------------------------------------------------
# Bulk dialog
# --------------------------------------------------------------------------

def test_bulk_dialog_lists_repos_and_paths():
    m.app.repos = {
        "r1": _repo("dctm-server-image"),
        "r2": _repo("dg6containerize"),
        "r3": _repo("dbkcontainerize"),
    }
    blocked = [
        ("r1", ["Dockerfile"]),
        ("r2", ["src/main.go", "go.mod"]),
        ("r3", ["README.md"]),
    ]
    text, buttons = _walk(m._show_pull_all_stash_prompt(blocked))
    check("bulk_names_every_repo",
          all(n in text for n in ("dctm-server-image", "dg6containerize",
                                  "dbkcontainerize")))
    check("bulk_names_paths", "Dockerfile" in text and "src/main.go" in text)
    check("bulk_confirm_counts", "Stash & pull 3" in buttons)
    check("bulk_has_cancel", "Cancel" in buttons)


def test_bulk_dialog_truncates_paths_per_repo():
    m.app.repos = {"r1": _repo("wide")}
    blocked = [("r1", ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"])]
    text, _ = _walk(m._show_pull_all_stash_prompt(blocked))
    check("bulk_shows_three_paths", "a.txt, b.txt, c.txt" in text)
    check("bulk_truncates_paths", "+2 more" in text)


def test_bulk_dialog_falls_back_to_folder_name():
    # A repo unwatched between the batch and the render must still render a
    # name rather than raise -- the handler filters, but the builder is the
    # last line of defence.
    m.app.repos = {}
    text, _ = _walk(m._show_pull_all_stash_prompt([("C:/repos/ghost", ["x.txt"])]))
    check("bulk_fallback_name", "ghost" in text)


if __name__ == "__main__":
    _setup_context()
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("\nAll tests passed.")
