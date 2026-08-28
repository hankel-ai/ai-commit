"""Push refused because the branch tracks a differently-named remote branch.

Git has TWO refusals for "this branch isn't tracking a same-named remote
branch", and ai-commit only recognised the first:

  1. no upstream at all ->
       fatal: The current branch X has no upstream branch.
  2. an upstream whose name differs (push.default=simple) ->
       fatal: The upstream branch of your current branch does not match
       the name of your current branch.

Only (1) reached `NO_UPSTREAM:` and the "Push & Set Upstream" prompt. (2) fell
through to a bare sticky error -- "Error: Push failed: fatal: The upstream
branch ..." -- with no button, even though the remedy is the same
`git push --set-upstream origin <branch>`. Seen on a GitLab repo whose local
`main_master` tracked `origin/zulu_master`, where the remote `main_master` did
not exist yet, so the user had no way to create it from the GUI.

The stderr blobs below are verbatim git output (git 2.x, push.default=simple).

Run: python tests/test_upstream_mismatch.py
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

# Neutralize the GUI's startup self-detach so exec_module just defines symbols.
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []

# Captured before any test stubs it out.
_REAL_PROMPT = m._show_upstream_prompt


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


# Captured from a real repro: local branch main_master tracking
# origin/zulu_master, bare `git push` (rc=128).
MISMATCH_STDERR = """fatal: The upstream branch of your current branch does not match
the name of your current branch.  To push to the upstream branch
on the remote, use

    git push origin HEAD:zulu_master

To push to the branch of the same name on the remote, use

    git push origin HEAD

To choose either option permanently, see push.default in 'git help config'.

To avoid automatically configuring an upstream branch when its name
won't match the local branch, see option 'simple' of branch.autoSetupMerge
in 'git help config'."""

NO_UPSTREAM_STDERR = """fatal: The current branch main_master has no upstream branch.
To push the current branch and set the remote as upstream, use

    git push --set-upstream origin main_master

To have this happen automatically for branches without a tracking
upstream, see 'push.autoSetupRemote' in 'git help config'."""

NON_FAST_FORWARD_STDERR = """ ! [rejected]        main -> main (non-fast-forward)
error: failed to push some refs to 'https://github.com/x/y.git'
hint: Updates were rejected because the tip of your current branch is behind"""


def test_needs_upstream_setup():
    check("detects_name_mismatch", core.needs_upstream_setup(MISMATCH_STDERR))
    check("detects_no_upstream", core.needs_upstream_setup(NO_UPSTREAM_STDERR))
    # Wrapping is git's, not ours -- a single-line variant must match too.
    check("detects_unwrapped_mismatch", core.needs_upstream_setup(
        "fatal: The upstream branch of your current branch does not match "
        "the name of your current branch."))
    check("ignores_non_fast_forward",
          not core.needs_upstream_setup(NON_FAST_FORWARD_STDERR))
    check("ignores_auth_failure", not core.needs_upstream_setup(
        "fatal: Authentication failed for 'https://git.example.com/x.git'"))
    check("ignores_empty", not core.needs_upstream_setup(""))


def test_parse_upstream_mismatch():
    check("parses_mismatched_upstream",
          core.parse_upstream_mismatch(MISMATCH_STDERR) == "zulu_master")
    # No mismatch to report when the branch simply has no upstream: that
    # stderr's suggestion is `--set-upstream origin <same name>`.
    check("no_upstream_has_no_mismatch",
          core.parse_upstream_mismatch(NO_UPSTREAM_STDERR) == "")
    check("empty_has_no_mismatch", core.parse_upstream_mismatch("") == "")


def _stub_core_git(push_rc, push_err, branch="main_master"):
    """Drive do_commit_and_push through a scripted git."""
    calls = []

    def spy_run_git(args, cwd=None, **kw):
        calls.append(args)
        if args[0] == "push":
            return push_rc, "", push_err
        if args[0] == "commit":
            return 0, "1 file changed", ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, branch, ""
        return 0, "", ""

    core.run_git = spy_run_git
    return calls


def test_do_commit_and_push_classifies_mismatch():
    orig = core.run_git
    try:
        _stub_core_git(128, MISMATCH_STDERR)
        committed, pushed, detail = core.do_commit_and_push("C:/repos/r1", "msg")
        check("mismatch_committed", committed and not pushed)
        # branch first, then the differently-named upstream (':' is illegal in
        # a git ref name, so this stays unambiguous).
        check("mismatch_detail", detail == "NO_UPSTREAM:main_master:zulu_master")

        _stub_core_git(128, NO_UPSTREAM_STDERR)
        _c, _p, detail = core.do_commit_and_push("C:/repos/r1", "msg")
        check("no_upstream_detail", detail == "NO_UPSTREAM:main_master")

        _stub_core_git(1, NON_FAST_FORWARD_STDERR)
        _c, _p, detail = core.do_commit_and_push("C:/repos/r1", "msg")
        check("ordinary_failure_not_classified",
              not detail.startswith("NO_UPSTREAM:") and "non-fast-forward" in detail)
    finally:
        core.run_git = orig


def _fake_repo(path):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main_master", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=m.GenStatus.IDLE, error_message="", commit_message="",
        input_tag=None, status_tag=None,
    )


class _NoopDpg:
    """Swallow widget calls -- real Dear PyGui segfaults with no context."""

    def __getattr__(self, name):
        return lambda *a, **kw: None


def _drive_queue(message, repo):
    """Run one queue message through the real process_queue."""
    import queue as _queue
    q = _queue.Queue()
    q.put(message)
    m.ui_queue = q
    prompts = []
    m._show_upstream_prompt = (
        lambda repo_name, branch, current_upstream="":
        prompts.append((repo_name, branch, current_upstream)))
    m.update_repo_status = lambda rs: None
    m.clear_commit_input = lambda rs: None
    m.executor = SimpleNamespace(submit=lambda fn, *a, **kw: None)
    m.app = m.AppState(repos={message[1]: repo}, non_git_folders={},
                            collapse_on_next_build=set())
    real_dpg, real_prompt = m.dpg, _REAL_PROMPT
    m.dpg = _NoopDpg()
    try:
        m.process_queue()
    finally:
        # Restore both -- test_prompt_renders_mismatch_wording needs the real
        # popup, and it runs after these.
        m.dpg = real_dpg
        m._show_upstream_prompt = real_prompt
    return prompts


def test_push_button_failure_prompts_for_upstream():
    """The path in the bug report: banner Push button -> bare `git push`."""
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, MISMATCH_STDERR, ""), rs)
    check("push_only_mismatch_prompts",
          prompts == [(r1, "main_master", "zulu_master")])
    check("push_only_mismatch_keeps_error",
          rs.gen_status == m.GenStatus.ERROR
          and "does not match" in rs.error_message)


def test_push_button_no_upstream_prompts():
    r1 = "C:/repos/r1"
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, NO_UPSTREAM_STDERR, ""),
        _fake_repo(r1))
    check("push_only_no_upstream_prompts",
          prompts == [(r1, "main_master", "")])


def test_set_upstream_push_failure_does_not_reprompt():
    """Guard against a prompt loop: this failure WAS the --set-upstream push."""
    r1 = "C:/repos/r1"
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, MISMATCH_STDERR, "main_master"),
        _fake_repo(r1))
    check("set_upstream_retry_no_prompt", prompts == [])


def test_ordinary_push_failure_does_not_prompt():
    r1 = "C:/repos/r1"
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, NON_FAST_FORWARD_STDERR, ""),
        _fake_repo(r1))
    check("ordinary_failure_no_prompt", prompts == [])


def test_commit_result_carries_mismatched_upstream():
    r1 = "C:/repos/r1"
    prompts = _drive_queue(
        ("commit_result", r1, True, False, "NO_UPSTREAM:main_master:zulu_master"),
        _fake_repo(r1))
    check("commit_result_mismatch_prompts",
          prompts == [(r1, "main_master", "zulu_master")])

    prompts = _drive_queue(
        ("commit_result", r1, True, False, "NO_UPSTREAM:main_master"),
        _fake_repo(r1))
    check("commit_result_no_upstream_prompts",
          prompts == [(r1, "main_master", "")])


def test_prompt_renders_mismatch_wording():
    """Real Dear PyGui render -- the popup must not claim 'no remote tracking
    branch' for a branch that IS tracking one under another name.

    Self-skips where a dpg context can't be created (headless CI).
    """
    import dearpygui.dearpygui as dpg
    try:
        dpg.create_context()
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"skip prompt_render ({exc})")
        return
    try:
        m.green_btn_theme = m.create_button_theme((50, 130, 75))
        m._show_upstream_prompt("C:/repos/r1", "main_master",
                                current_upstream="zulu_master")

        found = []

        def walk(item):
            if "Text" in dpg.get_item_type(item):
                found.append(dpg.get_value(item))
            for child in (dpg.get_item_children(item, 1) or []):
                walk(child)

        for top in dpg.get_all_items():
            if dpg.get_item_type(top) == "mvAppItemType::mvWindowAppItem":
                walk(top)
        blob = "\n".join(found)
        check("prompt_names_current_upstream", "origin/zulu_master" in blob)
        check("prompt_not_claiming_untracked",
              "has no remote tracking branch" not in blob)
        check("prompt_shows_set_upstream_command",
              "git push --set-upstream origin main_master" in blob)
    finally:
        dpg.destroy_context()


def main():
    test_needs_upstream_setup()
    test_parse_upstream_mismatch()
    test_do_commit_and_push_classifies_mismatch()
    test_push_button_failure_prompts_for_upstream()
    test_push_button_no_upstream_prompts()
    test_set_upstream_push_failure_does_not_reprompt()
    test_ordinary_push_failure_does_not_prompt()
    test_commit_result_carries_mismatched_upstream()
    test_prompt_renders_mismatch_wording()  # last: creates a real dpg context
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
