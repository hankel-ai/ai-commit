"""Server-side push-rule rejections: detect them apart from secret blocks.

GitLab declines a push through the same pre-receive hook for two very
different reasons:

  * secret push protection -- bypassable for one push with
    `-o secret_push_protection.skip_all` (see test_secret_push_override.py)
  * a push RULE (prohibited file name, commit-message regex, max file size)
    -- NOT bypassable by any push option

Offering the skip-override for the second kind sends the user round a loop
that cannot succeed, so the GUI must tell them apart and show the remote's own
explanation instead of a dead-end sticky error clipped at the panel edge.

Run: python tests/test_push_rule_block.py
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


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


# Verbatim shape of the real rejection (dxacontainerize, 2026-09-02): one long
# `remote:` line that runs off the panel, then git's own summary lines.
FILE_NAME_RULE_STDERR = (
    'remote: GitLab: File name helm/charts/apphost/secrets/dev/dfc.keystore'
    ' was prohibited by the pattern "(jks|keystore|key|pem)$".\n'
    'To https://git.delta.com/dctm/dxacontainerize.git\n'
    ' ! [remote rejected] main_master -> main_master (pre-receive hook declined)\n'
    "error: failed to push some refs to"
    " 'https://git.delta.com/dctm/dxacontainerize.git'"
)

SECRET_BLOCK_STDERR = """remote: GitLab:
remote: PUSH BLOCKED: Secrets detected in code changes
remote:
remote: To skip secret push protection, add the following Git push option to your push command: `-o secret_push_protection.skip_all`
To https://git.delta.com/dctm/doycontainerize.git
 ! [remote rejected] main_master -> main_master (pre-receive hook declined)
error: failed to push some refs to 'https://git.delta.com/dctm/doycontainerize.git'"""

PROTECTED_BRANCH_STDERR = (
    "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
    " ! [remote rejected] main -> main (protected branch hook declined)\n"
    "error: failed to push some refs to 'https://github.com/o/r.git'"
)


def test_detection():
    check("detects_file_name_rule", core.is_push_rule_block(FILE_NAME_RULE_STDERR))
    check("detects_protected_branch", core.is_push_rule_block(PROTECTED_BRANCH_STDERR))
    # The secret block has its own override path and must not be swallowed here.
    check("secret_block_is_not_a_push_rule",
          not core.is_push_rule_block(SECRET_BLOCK_STDERR))
    check("secret_block_still_detected",
          core.is_secret_push_block(SECRET_BLOCK_STDERR))
    check("ignores_non_fast_forward", not core.is_push_rule_block(
        "! [rejected] main -> main (non-fast-forward)\n"
        "error: failed to push some refs"))
    check("ignores_auth_failure", not core.is_push_rule_block(
        "fatal: Authentication failed for 'https://git.example.com/x.git'"))
    check("ignores_no_upstream", not core.is_push_rule_block(
        "fatal: The current branch feat has no upstream branch."))
    check("ignores_empty", not core.is_push_rule_block(""))


def test_remote_reject_reason():
    reason = core.remote_reject_reason(FILE_NAME_RULE_STDERR)
    check("reason_keeps_the_rule_text",
          "File name helm/charts/apphost/secrets/dev/dfc.keystore" in reason)
    check("reason_keeps_the_pattern", 'prohibited by the pattern' in reason)
    check("reason_strips_remote_prefix", "remote:" not in reason)
    check("reason_drops_local_git_lines",
          "failed to push some refs" not in reason
          and "[remote rejected]" not in reason)

    multi = core.remote_reject_reason(SECRET_BLOCK_STDERR)
    check("reason_drops_banner_only_lines", "GitLab:" not in multi.split("\n"))
    check("reason_keeps_every_remote_line",
          "PUSH BLOCKED: Secrets detected in code changes" in multi
          and "secret_push_protection.skip_all" in multi)

    # No `remote:` lines at all -> fall back to the raw text, never empty.
    check("reason_falls_back_to_raw",
          core.remote_reject_reason("fatal: boom").strip() == "fatal: boom")
    check("reason_empty_for_empty", core.remote_reject_reason("") == "")


def _fake_repo(path, gen_status=None, error_message=""):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main_master", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=gen_status if gen_status is not None else m.GenStatus.IDLE,
        error_message=error_message, commit_message="", input_tag=None,
        status_tag=None,
    )


def _drive_queue(message, repo):
    """Run one ui_queue message through process_queue, recording both prompts."""
    import queue as _queue
    q = _queue.Queue()
    q.put(message)
    m.ui_queue = q
    secret_prompts = []
    rule_prompts = []
    m._show_secret_push_prompt = (
        lambda repo_name, branch="": secret_prompts.append((repo_name, branch)))
    m._show_push_rule_prompt = (
        lambda repo_name, detail, branch="":
        rule_prompts.append((repo_name, detail, branch)))
    m.update_repo_status = lambda rs: None
    m.app = m.AppState(repos={message[1]: repo}, non_git_folders={},
                       collapse_on_next_build=set())
    m.process_queue()
    return secret_prompts, rule_prompts


def test_push_upstream_result_routes_to_rule_prompt():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    secret, rule = _drive_queue(
        ("push_upstream_result", r1, False, FILE_NAME_RULE_STDERR, "main_master"), rs)
    check("upstream_rule_prompts", len(rule) == 1 and rule[0][0] == r1)
    check("upstream_rule_carries_branch", rule and rule[0][2] == "main_master")
    check("upstream_rule_carries_detail",
          rule and "dfc.keystore" in rule[0][1])
    check("upstream_rule_no_secret_prompt", secret == [])
    check("upstream_rule_error_still_set",
          rs.gen_status == m.GenStatus.ERROR and "dfc.keystore" in rs.error_message)


def test_push_upstream_result_secret_block_unchanged():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    secret, rule = _drive_queue(
        ("push_upstream_result", r1, False, SECRET_BLOCK_STDERR, "feat/x"), rs)
    check("secret_still_wins", secret == [(r1, "feat/x")] and rule == [])


def test_commit_result_routes_to_rule_prompt():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    secret, rule = _drive_queue(
        ("commit_result", r1, True, False,
         "1 file changed\nPush failed: " + FILE_NAME_RULE_STDERR), rs)
    check("commit_rule_prompts", len(rule) == 1 and rule[0][0] == r1)
    check("commit_rule_branch_empty", rule and rule[0][2] == "")
    check("commit_rule_no_secret_prompt", secret == [])


def test_commit_result_ordinary_error_no_prompt():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    secret, rule = _drive_queue(
        ("commit_result", r1, True, False,
         "Push failed: ! [rejected] main -> main (non-fast-forward)"), rs)
    check("ordinary_error_no_prompt", secret == [] and rule == [])


def test_upstream_prompt_not_offered_for_rule_block():
    """A push-rule decline must not be mistaken for a missing upstream."""
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    upstream_prompts = []
    m._show_upstream_prompt = (
        lambda *a, **kw: upstream_prompts.append(a))
    _drive_queue(("push_upstream_result", r1, False, FILE_NAME_RULE_STDERR, ""), rs)
    check("no_upstream_prompt_for_rule_block", upstream_prompts == [])


def test_status_line_wraps_long_error():
    """The sticky error must wrap, not run off the right edge of the panel.

    The real bug: the one-line `remote: GitLab: File name ... was prohibited by
    the pattern ...` was clipped at the panel edge, so the reason was
    unreadable. update_repo_status must hard-wrap what it writes.
    """
    import dearpygui.dearpygui as dpg

    # The queue tests stub update_repo_status out on `m`, so load a pristine
    # copy of the module for this one.
    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)

    dpg.create_context()
    try:
        with dpg.window():
            tag = dpg.add_text("")
        rs = _fake_repo("C:/repos/r1", gen_status=fresh.GenStatus.ERROR,
                        error_message="Push failed: " + FILE_NAME_RULE_STDERR)
        rs.status_tag = tag
        fresh.app = fresh.AppState(repos={}, non_git_folders={})
        fresh.update_repo_status(rs)
        shown = dpg.get_value(tag)
        width = fresh._get_wrap_width()
        longest = max(len(ln) for ln in shown.split("\n"))
        check("error_is_wrapped", longest <= width)
        check("wrapped_error_keeps_the_reason",
              "prohibited by the pattern" in shown.replace("\n", " "))
    finally:
        dpg.destroy_context()


def test_push_rule_prompt_builds():
    """The popup must actually render -- it is the only place the reason
    becomes readable, so a typo in it would silently restore the dead end."""
    import dearpygui.dearpygui as dpg

    fresh = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(fresh)

    dpg.create_context()
    try:
        r1 = "C:/repos/dxacontainerize"
        rs = _fake_repo(r1)
        fresh.app = fresh.AppState(repos={r1: rs}, non_git_folders={})
        fresh._show_push_rule_prompt(r1, FILE_NAME_RULE_STDERR, "main_master")

        texts = []
        buttons = []
        for win in dpg.get_all_items():
            if not dpg.does_item_exist(win):
                continue
            kind = dpg.get_item_type(win)
            if kind == "mvAppItemType::mvText":
                texts.append(dpg.get_value(win) or "")
            elif kind == "mvAppItemType::mvButton":
                buttons.append(dpg.get_item_label(win))
        blob = "\n".join(texts)
        check("prompt_shows_the_rule_reason", "dfc.keystore" in blob)
        check("prompt_shows_the_pattern", "prohibited by the pattern" in blob)
        check("prompt_says_no_bypass", "no push option bypasses it" in blob)
        check("prompt_says_branch_not_created",
              "origin/main_master was NOT created" in blob)
        check("prompt_offers_copy_and_close",
              "Copy Error" in buttons and "Close" in buttons)
        # A retry button here would loop forever: nothing bypasses a push rule.
        check("prompt_offers_no_retry",
              not any("Push" in (b or "") for b in buttons))
    finally:
        dpg.destroy_context()


def main():
    test_detection()
    test_remote_reject_reason()
    test_push_upstream_result_routes_to_rule_prompt()
    test_push_upstream_result_secret_block_unchanged()
    test_commit_result_routes_to_rule_prompt()
    test_commit_result_ordinary_error_no_prompt()
    test_upstream_prompt_not_offered_for_rule_block()
    test_status_line_wraps_long_error()
    test_push_rule_prompt_builds()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
