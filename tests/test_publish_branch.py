"""Publish a local-only branch to the remote before it has any changes.

A branch created from the MORE panel (`git switch -c`) has no upstream, and the
only way to push it used to be Commit & Push -- which fails NO_UPSTREAM and
*then* offers "Push & Set Upstream". Commit & Push only renders for a dirty
tree, so a fresh branch with no changes could not be put on the remote at all.

Covers the pure predicate, a real Dear PyGui render of the banner button, and
the button's callback driving a real `git push --set-upstream` against a local
bare remote.

Run: python tests/test_publish_branch.py
"""
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

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


# --- predicate ---------------------------------------------------------------

def test_should_offer_publish():
    check("local_only_with_remote",
          core.should_offer_publish("local only", REMOTE))
    check("tracked_branch_no_publish", not core.should_offer_publish("", REMOTE))
    # Upstream deleted on the remote: a different situation, not this button.
    check("stale_no_publish", not core.should_offer_publish("stale", REMOTE))
    check("no_remote_no_publish", not core.should_offer_publish("local only", ""))


# --- real Dear PyGui render --------------------------------------------------

def _setup_context():
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
    )


def _section_items(entries, branch_status, remote=REMOTE, ahead=0):
    """Build one repo section; return (button labels, text values) inside it."""
    rs = m.RepoState(
        path=Path("C:/repos/hermes"), name="hermes", folder_name="hermes",
        entries=entries, remote_url=remote, branch="feature/x",
        branch_status=branch_status, ahead=ahead, behind=0,
    )
    m.app.repos[str(rs.path)] = rs
    with dpg.window() as win:
        m.build_repo_section(rs, win)
    labels, texts = [], []

    def walk(tag):
        for child_list in dpg.get_item_children(tag).values():
            for c in child_list:
                t = dpg.get_item_type(c)
                if t == "mvAppItemType::mvButton":
                    labels.append(dpg.get_item_label(c))
                elif t == "mvAppItemType::mvText":
                    texts.append(str(dpg.get_value(c)))
                walk(c)

    walk(win)
    return labels, texts


def test_publish_button_render():
    labels, texts = _section_items([], "local only")
    check("clean_local_only_offers_publish", "Publish" in labels)
    check("banner_names_branch",
          any("feature/x" in t and "not on the remote" in t for t in texts))

    labels, _ = _section_items([("M", "values.yaml")], "local only")
    check("dirty_local_only_offers_publish_and_commit",
          "Publish" in labels and "Commit & Push" in labels)

    labels, _ = _section_items([], "")
    check("tracked_no_publish", "Publish" not in labels)

    labels, _ = _section_items([], "stale")
    check("stale_no_publish_button", "Publish" not in labels)

    labels, _ = _section_items([], "local only", remote="")
    check("no_remote_no_publish_button", "Publish" not in labels)


def test_unpublished_branch_counts_as_active():
    # A fresh branch off an old commit is clean, has no ahead/behind and an
    # old last commit -- without this it is "idle" and Recent only hides it.
    old = 1_000_000.0
    now = old + 365 * 86400
    check("idle_without_unpublished",
          not core.is_repo_active(old, False, 0, 0, now, 14))
    check("unpublished_is_active",
          core.is_repo_active(old, False, 0, 0, now, 14, unpublished=True))


def test_recent_only_shows_unpublished_branch():
    """Real rebuild_repos_ui with Recent only on: the local-only repo with an
    old last commit must still render (with its Publish button)."""
    import time
    old = time.time() - 365 * 86400
    key = str(Path("C:/repos/hermes"))
    info = {"path": Path(key), "entries": [], "remote_url": REMOTE,
            "branch": "feature/x", "branch_status": "local only",
            "last_commit_ts": old, "last_commit_date": "", "ahead": 0,
            "behind": 0}
    m.app = SimpleNamespace(
        repos={}, non_git_folders={}, active_gh_account="hankel-ai",
        expand_on_next_build=set(), collapse_on_next_build=set(),
        repo_overrides={}, paused=False, expanded_changes=set(),
        typeahead_buf="", typeahead_miss=False,
        recent_only=True, recent_days=14, sort_by_date=False,
        show_non_git_folders=True, auto_generate=False, poll_pending=set(),
        last_results={key: info}, last_non_git={},
    )
    with dpg.window(tag="primary"):
        dpg.add_text("", tag="typeahead_label")
        dpg.add_text("", tag="hidden_count_label")
        with dpg.child_window(tag="repos_container"):
            pass
    m.rebuild_repos_ui(m.app.last_results, m.app.last_non_git)
    rs = m.app.repos.get(key)
    children = set(dpg.get_item_children("repos_container", 1) or [])
    check("recent_only_keeps_unpublished_repo",
          rs is not None and rs.header_tag in children)
    check("hidden_count_zero", dpg.get_value("hidden_count_label") == "")


# --- real git: the button publishes the branch -------------------------------

def _rmtree(path):
    def _force(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_force)


def _git(cwd, *args):
    rc, out, err = core.run_git(list(args), cwd=cwd)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {err.strip()}")
    return out


def test_publish_pushes_branch():
    root = tempfile.mkdtemp(prefix="aic_publish_")
    try:
        _git(root, "init", "--bare", "--initial-branch=main", "origin.git")
        origin = os.path.join(root, "origin.git")
        work = os.path.join(root, "work")
        os.makedirs(work)
        _git(work, "init", "--initial-branch=main")
        _git(work, "config", "user.email", "test@example.com")
        _git(work, "config", "user.name", "Test")
        with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("hi\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "initial")
        _git(work, "remote", "add", "origin", origin)
        _git(work, "push", "-u", "origin", "main")

        # What the MORE panel's Create does: new branch, no changes.
        _git(work, "switch", "-c", "feature/x")
        entries, branch, ahead, behind, status = m._read_poll_status(
            work, origin, False)
        check("fresh_branch_is_local_only",
              branch == "feature/x" and status == "local only" and not entries)

        rs = SimpleNamespace(path=Path(work), name="work", branch=branch,
                             branch_status=status, status_tag=None)
        m.app = SimpleNamespace(repos={work: rs}, non_git_folders={})
        posted = []
        m.ui_queue = SimpleNamespace(put=posted.append)
        m.executor = SimpleNamespace(submit=lambda fn, *a: fn(*a))

        m.cb_publish_branch(None, None, work)

        check("posts_push_upstream_success",
              posted and posted[0][0] == "push_upstream_result"
              and posted[0][2] is True)
        remote_head = _git(work, "ls-remote", "origin", "refs/heads/feature/x")
        check("branch_exists_on_remote", "refs/heads/feature/x" in remote_head)
        _e, _b, _a, _bh, status_after = m._read_poll_status(work, origin, False)
        check("branch_now_tracks_remote", status_after == "")
    finally:
        _rmtree(root)


def main():
    test_should_offer_publish()
    test_unpublished_branch_counts_as_active()
    test_publish_pushes_branch()
    try:
        _setup_context()
    except Exception as exc:  # no GPU/display available
        print(f"SKIP render: cannot create a Dear PyGui context here ({exc})")
    else:
        try:
            test_publish_button_render()
            test_recent_only_shows_unpublished_branch()
        finally:
            dpg.destroy_context()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
