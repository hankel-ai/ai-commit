"""Push-only retry for a commit that landed but whose push failed.

Regression for: a push rejected by a transient server error (e.g. GitHub's
`remote: Internal Server Error`) leaves the commit in place and the tree clean.
The Commit & Push button lives inside `if rs.entries:` in build_repo_section,
so a clean tree renders no button row at all, and the "PUSH REQUIRED" banner
was plain text -- unlike the "behind" banner, which carries a Preview Pull
button. The result was an unpushed commit with no way to retry the push from
the GUI, and a manual per-repo Refresh only re-rendered the same dead end.

Run: python tests/test_push_only.py
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


GITHUB_500_STDERR = """remote: Internal Server Error
remote: Request ID FEE4:2836D5:262D22A:35A80FB:6A73501B
remote: Time 2026-08-05T15:00:43Z
To https://github.com/hankel-ai/hermes.git
 ! [remote rejected] main -> main (Internal Server Error)
error: failed to push some refs to 'https://github.com/hankel-ai/hermes.git'"""


def test_should_offer_push():
    # The ordinary case this fixes: commit landed, push failed, tree clean.
    check("ahead_with_remote", core.should_offer_push(1, 0, "https://x/y.git"))
    check("ahead_many", core.should_offer_push(3, 0, "https://x/y.git"))
    # Nothing to push.
    check("synced_no_button", not core.should_offer_push(0, 0, "https://x/y.git"))
    # Behind: a plain push would be rejected non-fast-forward. That banner
    # already offers Preview Pull instead.
    check("behind_no_button", not core.should_offer_push(1, 2, "https://x/y.git"))
    check("behind_only_no_button", not core.should_offer_push(0, 2, "https://x/y.git"))
    # No remote configured -- nowhere to push to.
    check("no_remote_no_button", not core.should_offer_push(1, 0, ""))


def _fake_repo(path, gen_status=None, error_message=""):
    return SimpleNamespace(
        path=Path(path), name=Path(path).name, entries=[],
        remote_url="https://github.com/hankel-ai/hermes.git",
        github_account="", visibility="", branch="main", branch_status="",
        last_commit_msg="", last_commit_date="", ahead=1, behind=0,
        gen_status=gen_status if gen_status is not None else m.GenStatus.IDLE,
        error_message=error_message, commit_message="", input_tag=None,
        status_tag=None,
    )


def _spy_gui(repo_key, rc, out, err):
    """Point the GUI module at a fake repo and a recording run_git/ui_queue."""
    m.app = SimpleNamespace(repos={repo_key: _fake_repo(repo_key)},
                            non_git_folders={})
    posted = []
    m.ui_queue = SimpleNamespace(put=lambda item: posted.append(item))
    calls = []

    def spy_run_git(args, cwd=None, **kw):
        calls.append(args)
        return rc, out, err

    m.run_git = spy_run_git
    return calls, posted


def test_bg_push_only_command():
    r1 = "C:/repos/r1"
    calls, posted = _spy_gui(r1, 0, "pushed", "")

    m.bg_push_only(r1)
    check("plain_push_args", calls[-1] == ["push"])

    results = [x for x in posted if x[0] == "push_upstream_result"]
    check("posts_push_upstream_result", len(results) == 1)
    check("posts_success_flag", results[0][2] is True)


def test_bg_push_only_failure_posts_error():
    r1 = "C:/repos/r1"
    calls, posted = _spy_gui(r1, 1, "", GITHUB_500_STDERR)

    m.bg_push_only(r1)
    results = [x for x in posted if x[0] == "push_upstream_result"]
    check("failure_posted", len(results) == 1 and results[0][2] is False)
    check("failure_carries_stderr", "Internal Server Error" in results[0][3])
    # No branch: this is a retry of a push that already had an upstream, so a
    # secret-block override must not add --set-upstream.
    check("failure_carries_empty_branch",
          len(results[0]) > 4 and results[0][4] == "")


def test_bg_push_only_missing_repo_is_noop():
    calls, posted = _spy_gui("C:/repos/r1", 0, "pushed", "")
    m.bg_push_only("C:/repos/gone")
    check("unknown_repo_no_git", calls == [])
    check("unknown_repo_no_post", posted == [])


def test_cb_push_now_submits_bg_push_only():
    r1 = "C:/repos/r1"
    m.app = SimpleNamespace(repos={r1: _fake_repo(r1)}, non_git_folders={})
    submitted = []
    m.executor = SimpleNamespace(
        submit=lambda fn, *a: submitted.append((fn, a)))

    m.cb_push_now(None, None, r1)
    check("cb_submits_bg_push_only",
          submitted == [(m.bg_push_only, (r1,))])


def main():
    test_should_offer_push()
    test_bg_push_only_command()
    test_bg_push_only_failure_posts_error()
    test_bg_push_only_missing_repo_is_noop()
    test_cb_push_now_submits_bg_push_only()
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
