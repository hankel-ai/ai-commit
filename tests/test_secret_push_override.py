"""GitLab secret-push-protection block: detect it and offer a skip-override.

When a push is rejected by GitLab's secret push protection (pre-receive hook
prints "PUSH BLOCKED: Secrets detected in code changes" and suggests
`-o secret_push_protection.skip_all`), the GUI should prompt the user to
re-push with that option instead of leaving only a dead-end sticky error.

Run: python tests/test_secret_push_override.py
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


GITLAB_BLOCK_STDERR = """remote: GitLab:
remote: PUSH BLOCKED: Secrets detected in code changes
remote:
remote: Secret push protection found the following secrets in commit: 5f75c507ae0479cfd52efaa75ca101629
remote: -- helm/charts/dctm-server/charts/cs-secrets/values.yaml:359 | Google (GCP) service account
remote:
remote: To push your changes you must remove the identified secrets.
remote: To skip secret push protection, add the following Git push option to your push command: `-o secret_push_protection.skip_all`
To https://git.delta.com/dctm/doycontainerize.git
 ! [remote rejected] main_master -> main_master (pre-receive hook declined)
error: failed to push some refs to 'https://git.delta.com/dctm/doycontainerize.git'"""


def test_detection():
    check("detects_gitlab_block", core.is_secret_push_block(GITLAB_BLOCK_STDERR))
    check("detects_skip_option_mention",
          core.is_secret_push_block("add `-o secret_push_protection.skip_all`"))
    check("ignores_non_fast_forward", not core.is_secret_push_block(
        "! [rejected] main -> main (non-fast-forward)\n"
        "error: failed to push some refs"))
    check("ignores_auth_failure", not core.is_secret_push_block(
        "fatal: Authentication failed for 'https://git.example.com/x.git'"))
    check("ignores_empty", not core.is_secret_push_block(""))


def _fake_repo(path, gen_status=None, error_message=""):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[], remote_url="",
        github_account="", visibility="", branch="main_master", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=gen_status if gen_status is not None else m.GenStatus.IDLE,
        error_message=error_message, commit_message="", input_tag=None,
        status_tag=None,
    )


def test_bg_push_override_command():
    r1 = "C:/repos/r1"
    m.app = SimpleNamespace(repos={r1: _fake_repo(r1)}, non_git_folders={})
    posted = []
    m.ui_queue = SimpleNamespace(put=lambda item: posted.append(item))
    calls = []

    def spy_run_git(args, cwd=None, **kw):
        calls.append(args)
        return 0, "pushed", ""

    m.run_git = spy_run_git

    m.bg_push_override(r1)
    check("plain_override_args",
          calls[-1] == ["push", "-o", "secret_push_protection.skip_all"])

    m.bg_push_override(r1, "feat/x")
    check("upstream_override_args",
          calls[-1] == ["push", "-o", "secret_push_protection.skip_all",
                        "--set-upstream", "origin", "feat/x"])

    results = [x for x in posted if x[0] == "push_upstream_result"]
    check("posts_push_upstream_result", len(results) == 2)
    check("posts_success_flag", all(x[2] is True for x in results))


def _drive_queue(message, repo):
    import queue as _queue
    q = _queue.Queue()
    q.put(message)
    m.ui_queue = q
    prompts = []
    m._show_secret_push_prompt = (
        lambda repo_name, branch="": prompts.append((repo_name, branch)))
    m.update_repo_status = lambda rs: None
    m.app = SimpleNamespace(repos={message[1]: repo}, non_git_folders={},
                            collapse_on_next_build=set())
    m.process_queue()
    return prompts


def test_commit_result_block_prompts_override():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    prompts = _drive_queue(
        ("commit_result", r1, True, False, "1 file changed\nPush failed: "
         + GITLAB_BLOCK_STDERR), rs)
    check("commit_result_prompts", prompts == [(r1, "")])
    check("commit_result_error_still_set",
          rs.gen_status == m.GenStatus.ERROR and "PUSH BLOCKED" in rs.error_message)


def test_commit_result_ordinary_error_no_prompt():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    prompts = _drive_queue(
        ("commit_result", r1, True, False,
         "Push failed: ! [rejected] main -> main (non-fast-forward)"), rs)
    check("ordinary_error_no_prompt", prompts == [])


def test_push_upstream_result_block_prompts_with_branch():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, GITLAB_BLOCK_STDERR, "feat/x"), rs)
    check("upstream_block_prompts_with_branch", prompts == [(r1, "feat/x")])


def test_push_upstream_result_ordinary_error_no_prompt():
    r1 = "C:/repos/r1"
    rs = _fake_repo(r1)
    prompts = _drive_queue(
        ("push_upstream_result", r1, False, "Authentication failed", "feat/x"), rs)
    check("upstream_ordinary_no_prompt", prompts == [])


def main():
    test_detection()
    test_bg_push_override_command()
    test_commit_result_block_prompts_override()
    test_commit_result_ordinary_error_no_prompt()
    test_push_upstream_result_block_prompts_with_branch()
    test_push_upstream_result_ordinary_error_no_prompt()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
