"""Tests for the pull autostash path.

When `git pull` refuses because locally-modified files would be overwritten by
the merge, ai-commit offers to stash, pull, and restore. Two halves are covered
here:

* the pure detection/parsing of git's refusal text (`is_pull_blocked_by_local_changes`,
  `parse_pull_blocked_paths`), and
* the real stash -> pull -> pop orchestration (`do_pull_with_autostash`) driven
  against **real temp git repositories with a real remote**. Nothing about git
  is mocked: the point is to prove the sequence behaves on an actual index, so
  a stubbed `run_git` would prove nothing.

Run: python tests/test_pull_autostash.py
"""
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

_failures = []


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


# --- detection / parsing ----------------------------------------------------

# Verbatim git output, tabs and all. This is the exact refusal the GUI showed
# when a watched repo was 2 commits behind with a modified Dockerfile.
BLOCKED_TRACKED = (
    "error: Your local changes to the following files would be overwritten by merge:\n"
    "\tDockerfile\n"
    "Please commit your changes or stash them before you merge.\n"
    "Aborting\n"
)

BLOCKED_MULTI = (
    "error: Your local changes to the following files would be overwritten by merge:\n"
    "\tsrc/main.go\n"
    "\tgo.mod\n"
    "\tcharts/app/values.yaml\n"
    "Please commit your changes or stash them before you merge.\n"
    "Aborting\n"
)

BLOCKED_UNTRACKED = (
    "error: The following untracked working tree files would be overwritten by merge:\n"
    "\tdocs/new-page.md\n"
    "Please move or remove them before you merge.\n"
    "Aborting\n"
)


def test_detects_tracked_refusal():
    check("detect_tracked", core.is_pull_blocked_by_local_changes(BLOCKED_TRACKED))


def test_detects_untracked_refusal():
    check("detect_untracked", core.is_pull_blocked_by_local_changes(BLOCKED_UNTRACKED))


def test_detects_through_do_pull_prefix():
    # do_pull returns "git pull failed: <stderr>", so detection runs on the
    # already-prefixed detail string the UI holds -- not on raw stderr.
    detail = f"git pull failed: {BLOCKED_TRACKED.strip()}"
    check("detect_with_prefix", core.is_pull_blocked_by_local_changes(detail))


def test_ignores_unrelated_failures():
    others = [
        "git pull failed: fatal: could not read Username for 'https://github.com': No such device or address",
        "git pull failed: fatal: repository 'https://example.invalid/x.git' not found",
        "git pull failed: hint: You have divergent branches and need to specify how to reconcile them.",
        "git pull failed: CONFLICT (content): Merge conflict in Dockerfile\nAutomatic merge failed",
        "",
    ]
    check("ignore_unrelated",
          not any(core.is_pull_blocked_by_local_changes(t) for t in others))


def test_parses_single_path():
    check("parse_single",
          core.parse_pull_blocked_paths(BLOCKED_TRACKED) == ["Dockerfile"])


def test_parses_multiple_paths_in_order():
    check("parse_multi",
          core.parse_pull_blocked_paths(BLOCKED_MULTI)
          == ["src/main.go", "go.mod", "charts/app/values.yaml"])


def test_parses_untracked_paths():
    check("parse_untracked",
          core.parse_pull_blocked_paths(BLOCKED_UNTRACKED) == ["docs/new-page.md"])


def test_parse_stops_at_trailer():
    # "Please commit..." / "Aborting" must never be mistaken for filenames.
    paths = core.parse_pull_blocked_paths(BLOCKED_TRACKED)
    check("parse_no_trailer",
          not any("Aborting" in p or "Please" in p for p in paths))


def test_parse_handles_both_blocks():
    both = BLOCKED_TRACKED + BLOCKED_UNTRACKED
    check("parse_both_blocks",
          core.parse_pull_blocked_paths(both) == ["Dockerfile", "docs/new-page.md"])


def test_parse_empty_on_unrelated():
    check("parse_empty", core.parse_pull_blocked_paths("fatal: not a git repo") == [])


def test_marker_matches_lookup():
    # The marker the pull path writes must be the one find_autostash_ref finds,
    # or a pull stash would be invisible to the restore step.
    marker = core.autostash_marker("main")
    listing = f"stash@{{0}}: On main: {marker}\n"
    check("marker_roundtrip", core.find_autostash_ref(listing, "main") == "stash@{0}")


# --- real-git orchestration -------------------------------------------------

def _rm(path):
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


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# A file long enough that an edit at the top and an edit at the bottom merge
# cleanly -- that is the difference between the "restored" and "conflict" cases.
BASE_LINES = [f"line {i}\n" for i in range(1, 21)]


def _make_pair():
    """Build origin.git + two clones sharing it. Returns (root, upstream, work).

    `upstream` is used to publish commits; `work` is the repo under test.
    """
    root = tempfile.mkdtemp(prefix="aic_pull_")
    upstream = os.path.join(root, "upstream")
    origin = os.path.join(root, "origin.git")
    os.makedirs(upstream)

    _git(root, "init", "--bare", "--initial-branch=main", "origin.git")
    _git(upstream, "init", "--initial-branch=main")
    for cwd in (upstream,):
        _git(cwd, "config", "user.email", "test@example.com")
        _git(cwd, "config", "user.name", "Test")

    _write(os.path.join(upstream, "Dockerfile"), "".join(BASE_LINES))
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "initial")
    _git(upstream, "remote", "add", "origin", origin)
    _git(upstream, "push", "-u", "origin", "main")

    _git(root, "clone", origin, "work")
    work = os.path.join(root, "work")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    return root, upstream, work


def _publish_remote_edit(upstream, line_index, text):
    lines = list(BASE_LINES)
    lines[line_index] = text
    _write(os.path.join(upstream, "Dockerfile"), "".join(lines))
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-m", "remote edit")
    _git(upstream, "push", "origin", "main")


def _local_edit(work, line_index, text):
    lines = list(BASE_LINES)
    lines[line_index] = text
    _write(os.path.join(work, "Dockerfile"), "".join(lines))


def test_real_pull_is_blocked_and_parsed():
    """The screenshot scenario, reproduced end to end against real git."""
    root, upstream, work = _make_pair()
    try:
        _publish_remote_edit(upstream, 0, "REMOTE TOP\n")
        _local_edit(work, 19, "LOCAL BOTTOM\n")
        _git(work, "fetch", "origin")

        ok, detail = core.do_pull(work)
        check("real_blocked_fails", not ok)
        check("real_blocked_detected",
              core.is_pull_blocked_by_local_changes(detail))
        check("real_blocked_path",
              core.parse_pull_blocked_paths(detail) == ["Dockerfile"])
    finally:
        _rm(root)


def test_autostash_pull_restores_clean():
    """Stash -> pull -> pop round trip keeps the local edit AND the remote one."""
    root, upstream, work = _make_pair()
    try:
        _publish_remote_edit(upstream, 0, "REMOTE TOP\n")
        _local_edit(work, 19, "LOCAL BOTTOM\n")

        seen = []
        ok, detail, outcome = core.do_pull_with_autostash(
            work, progress=seen.append)

        check("restore_ok", ok)
        check("restore_outcome", outcome == core.PULL_AUTOSTASH_RESTORED)
        body = _read(os.path.join(work, "Dockerfile"))
        check("restore_kept_local", "LOCAL BOTTOM" in body)
        check("restore_got_remote", "REMOTE TOP" in body)
        check("restore_stash_gone", _git(work, "stash", "list").strip() == "")
        check("restore_progress", seen == ["Stashing...", "Pulling...", "Restoring..."])
        check("restore_detail_nonempty", bool(detail))
    finally:
        _rm(root)


def test_autostash_pop_conflict_keeps_stash():
    """A pop that conflicts must leave the stash in place, not drop it."""
    root, upstream, work = _make_pair()
    try:
        # Both sides edit the SAME line -> the pop cannot merge.
        _publish_remote_edit(upstream, 0, "REMOTE TOP\n")
        _local_edit(work, 0, "LOCAL TOP\n")

        ok, detail, outcome = core.do_pull_with_autostash(work)

        # The pull itself succeeded -- only the restore conflicted.
        check("conflict_pull_ok", ok)
        check("conflict_outcome", outcome == core.PULL_AUTOSTASH_CONFLICT)
        check("conflict_stash_kept",
              "ai-commit-autostash:main" in _git(work, "stash", "list"))
        check("conflict_detail_mentions_stash", "stash" in detail.lower())
        check("conflict_head_has_remote",
              "REMOTE TOP" in _git(work, "show", "HEAD:Dockerfile"))
    finally:
        _rm(root)


def test_failed_pull_rolls_the_stash_back():
    """If the pull fails after stashing, the tree must be left exactly as found."""
    root, upstream, work = _make_pair()
    try:
        _local_edit(work, 19, "LOCAL BOTTOM\n")
        before = _read(os.path.join(work, "Dockerfile"))
        # Break the remote so the pull fails for a reason stashing cannot fix.
        _git(work, "remote", "set-url", "origin",
             os.path.join(root, "does-not-exist.git"))

        ok, detail, outcome = core.do_pull_with_autostash(work)

        check("rollback_reports_failure", not ok)
        check("rollback_outcome", outcome == core.PULL_AUTOSTASH_ROLLED_BACK)
        check("rollback_tree_restored",
              _read(os.path.join(work, "Dockerfile")) == before)
        check("rollback_stash_gone", _git(work, "stash", "list").strip() == "")
    finally:
        _rm(root)


def test_clean_tree_pulls_without_stashing():
    """Nothing to stash -> plain pull, and no stray stash entry left behind."""
    root, upstream, work = _make_pair()
    try:
        _publish_remote_edit(upstream, 0, "REMOTE TOP\n")

        ok, detail, outcome = core.do_pull_with_autostash(work)

        check("clean_ok", ok)
        check("clean_outcome", outcome == core.PULL_AUTOSTASH_NOTHING)
        check("clean_no_stash", _git(work, "stash", "list").strip() == "")
        check("clean_got_remote",
              "REMOTE TOP" in _read(os.path.join(work, "Dockerfile")))
    finally:
        _rm(root)


def test_untracked_file_is_stashed_too():
    """--include-untracked: an untracked file blocking the merge is stashed."""
    root, upstream, work = _make_pair()
    try:
        # Remote adds a file the local tree already has untracked.
        _write(os.path.join(upstream, "NOTES.md"), "from remote\n")
        _git(upstream, "add", "-A")
        _git(upstream, "commit", "-m", "add notes")
        _git(upstream, "push", "origin", "main")
        _write(os.path.join(work, "NOTES.md"), "mine, untracked\n")

        ok, detail, outcome = core.do_pull_with_autostash(work)

        check("untracked_ok", ok)
        # Remote's version landed; the local untracked copy conflicts on pop.
        check("untracked_outcome",
              outcome in (core.PULL_AUTOSTASH_RESTORED,
                          core.PULL_AUTOSTASH_CONFLICT))
        check("untracked_remote_commit_present",
              "NOTES.md" in _git(work, "show", "--name-only", "HEAD"))
    finally:
        _rm(root)


def test_detached_head_is_refused():
    """No branch means no marker to tag the stash with -- refuse, don't guess."""
    root, upstream, work = _make_pair()
    try:
        sha = _git(work, "rev-parse", "HEAD").strip()
        _git(work, "checkout", sha)
        _local_edit(work, 19, "LOCAL BOTTOM\n")

        ok, detail, outcome = core.do_pull_with_autostash(work)

        check("detached_refused", not ok)
        check("detached_outcome", outcome == core.PULL_AUTOSTASH_DETACHED)
        check("detached_no_stash", _git(work, "stash", "list").strip() == "")
    finally:
        _rm(root)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    if _failures:
        print(f"\n{len(_failures)} check(s) FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("\nAll tests passed.")
