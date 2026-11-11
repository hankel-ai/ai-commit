"""Commit-identity guard: pinned hosts block, every other host warns.

Every case runs the real ``git`` binary against real temp repos. The global
config is a temp file (``GIT_CONFIG_GLOBAL``), so the user's own ~/.gitconfig is
never read or touched, and conditional includes are exercised exactly as git
evaluates them.

Push cases use a real local bare repo behind a fake GitLab host: the remote URL
stays ``https://gitlab.example.com/...`` (that is what the guard keys on) while
``url.<bare>.insteadOf`` makes git actually push to the bare repo.

Run: python tests/test_identity_guard.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ai_commit_core as core

os.environ["_AI_COMMIT_GUI_CHILD"] = "1"
_spec = importlib.util.spec_from_file_location(
    "acg", os.path.join(_ROOT, "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []

HOST = "gitlab.example.com"
WORK_URL = f"https://{HOST}/grp/proj.git"
WORK = ("Work Name", "work@corp.com")
PERSONAL = ("Personal Name", "personal@home.com")
COLLEAGUE = ("Colleague", "colleague@corp.com")
PINS = [{"host": HOST, "name": WORK[0], "email": WORK[1]}]


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def git(cwd, *args, ident=None):
    cmd = ["git"]
    if ident:
        cmd += ["-c", f"user.name={ident[0]}", "-c", f"user.email={ident[1]}"]
    res = subprocess.run(cmd + list(args), cwd=str(cwd), capture_output=True,
                         text=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {args} failed: {res.stderr}")
    return res.stdout.strip()


class Sandbox:
    """Temp dir with its own global git config (personal identity)."""

    def __init__(self, include_work=True):
        self.root = Path(tempfile.mkdtemp(prefix="ident-guard-"))
        self.global_cfg = self.root / "global.cfg"
        work_cfg = self.root / "work.cfg"
        work_cfg.write_text(
            f"[user]\n\tname = {WORK[0]}\n\temail = {WORK[1]}\n")
        text = (f"[user]\n\tname = {PERSONAL[0]}\n\temail = {PERSONAL[1]}\n"
                "[init]\n\tdefaultBranch = main\n")
        if include_work:
            path = work_cfg.as_posix()
            text += (f'[includeIf "hasconfig:remote.*.url:https://{HOST}/**"]\n'
                     f"\tpath = {path}\n")
        self.global_cfg.write_text(text)
        self._old = {k: os.environ.get(k)
                     for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.global_cfg)
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

    def repo(self, name, url=WORK_URL, commits=1, ident=None):
        path = self.root / name
        path.mkdir()
        git(path, "init", "-q")
        if url:
            git(path, "remote", "add", "origin", url)
        for i in range(commits):
            (path / f"f{i}.txt").write_text(str(i))
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", f"c{i}", ident=ident)
        return path

    def bare_behind(self, repo, url=WORK_URL):
        """Make *url* really push to a local bare repo; mark HEAD as pushed."""
        bare = self.root / (repo.name + "-remote.git")
        git(self.root, "init", "-q", "--bare", str(bare))
        git(repo, "config", f"url.{bare.as_posix()}.insteadOf", url)
        git(repo, "push", "-q", "-u", "origin", "HEAD")
        return bare

    def close(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)


# --- pure helpers ---------------------------------------------------------

def test_remote_host():
    check("host_https", core.remote_host("https://gitlab.example.com/a/b.git")
          == "gitlab.example.com")
    check("host_https_user",
          core.remote_host("https://jdoe@GitLab.Example.com/a/b") == "gitlab.example.com")
    check("host_https_port",
          core.remote_host("https://gitlab.example.com:8443/a/b") == "gitlab.example.com")
    check("host_scp", core.remote_host("git@github.com:o/r.git") == "github.com")
    check("host_ssh_url",
          core.remote_host("ssh://git@host.x:2222/o/r.git") == "host.x")
    check("host_local_path", core.remote_host("C:/repos/x.git") == "")
    check("host_unc_path", core.remote_host("//server/share/x.git") == "")
    check("host_empty", core.remote_host("") == "")


def test_normalize_pins():
    raw = [{"host": " GitLab.Example.com ", "name": " W ", "email": " w@x "},
           {"host": "", "name": "n", "email": "e"},
           {"host": "https://jdoe@other.host/x", "name": "", "email": "o@x"},
           "junk", None]
    pins = core.normalize_identity_pins(raw)
    check("pins_kept_two", len(pins) == 2)
    check("pins_host_lowered", pins[0]["host"] == "gitlab.example.com")
    check("pins_stripped", pins[0]["name"] == "W" and pins[0]["email"] == "w@x")
    check("pins_url_to_host", pins[1]["host"] == "other.host")
    check("pins_non_list", core.normalize_identity_pins("x") == [])
    check("find_pin_case", core.find_identity_pin(pins, "GITLAB.example.com")
          is pins[0])
    check("find_pin_none", core.find_identity_pin(pins, "github.com") is None)


# --- commit-time check ----------------------------------------------------

def test_commit_check_pinned_ok_via_include():
    sb = Sandbox()
    try:
        repo = sb.repo("ok")
        check("commit_pinned_include_ok",
              core.check_commit_identity(str(repo), PINS) is None)
    finally:
        sb.close()


def test_commit_check_pinned_local_override_blocks():
    sb = Sandbox()
    try:
        repo = sb.repo("override")
        git(repo, "config", "user.email", "oops@local.com")
        res = core.check_commit_identity(str(repo), PINS)
        check("override_blocks", res is not None and res["level"] == core.IDENTITY_BLOCK)
        check("override_names_host", res and res["host"] == HOST)
        check("override_names_origin", res and ".git/config" in res["message"])
        check("override_names_actual", res and "oops@local.com" in res["message"])
        check("override_fix_unsets", res and "git config --unset user.email" in res["fix"])
        check("override_name_untouched", res and "user.name" not in res["fix"])
        check("override_fix_args",
              res and res["fix_args"] == [["config", "--unset", "user.email"]])
        after = core.apply_identity_fix(str(repo), res, PINS,
                                        hub_path=sb.root / "hub" / "h.gitconfig")
        check("override_apply_fixes", after is None)
        check("override_apply_removed_local",
              subprocess.run(["git", "config", "--local", "user.email"], cwd=repo,
                             capture_output=True).returncode != 0)
    finally:
        sb.close()


def test_commit_check_without_include_apply_syncs():
    sb = Sandbox(include_work=False)
    try:
        repo = sb.repo("noinclude-apply")
        res = core.check_commit_identity(str(repo), PINS)
        check("noinclude_needs_sync", res and res.get("needs_sync") is True)
        after = core.apply_identity_fix(str(repo), res, PINS,
                                        hub_path=sb.root / "hub" / "h.gitconfig")
        check("noinclude_apply_fixes", after is None)
        check("noinclude_apply_no_local_identity",
              subprocess.run(["git", "config", "--local", "user.email"], cwd=repo,
                             capture_output=True).returncode != 0)
    finally:
        sb.close()


def test_commit_check_pinned_without_include_blocks():
    sb = Sandbox(include_work=False)
    try:
        repo = sb.repo("noinclude")
        res = core.check_commit_identity(str(repo), PINS)
        check("noinclude_blocks", res is not None and res["level"] == core.IDENTITY_BLOCK)
        check("noinclude_fix_sets_expected",
              res and f'git config user.email "{WORK[1]}"' in res["fix"]
              and f'git config user.name "{WORK[0]}"' in res["fix"])
    finally:
        sb.close()


def test_commit_check_email_case_insensitive():
    sb = Sandbox()
    try:
        repo = sb.repo("case")
        git(repo, "config", "user.email", WORK[1].upper())
        check("email_case_ignored",
              core.check_commit_identity(str(repo), PINS) is None)
    finally:
        sb.close()


def test_commit_check_pin_without_name_checks_email_only():
    sb = Sandbox()
    try:
        repo = sb.repo("noname")
        git(repo, "config", "user.name", "Someone Else")
        pins = [{"host": HOST, "name": "", "email": WORK[1]}]
        check("empty_pin_name_skipped",
              core.check_commit_identity(str(repo), pins) is None)
    finally:
        sb.close()


def test_commit_check_other_host_warns():
    sb = Sandbox()
    try:
        repo = sb.repo("gh", url="https://github.com/o/r.git")
        check("other_host_global_ok",
              core.check_commit_identity(str(repo), PINS) is None)
        git(repo, "config", "user.email", "oops@local.com")
        res = core.check_commit_identity(str(repo), PINS)
        check("other_host_warns", res is not None and res["level"] == core.IDENTITY_WARN)
        check("other_host_mentions_global", res and PERSONAL[1] in res["message"])
    finally:
        sb.close()


def test_commit_check_no_remote_warns():
    sb = Sandbox()
    try:
        repo = sb.repo("local", url=None)
        check("no_remote_ok", core.check_commit_identity(str(repo), PINS) is None)
        git(repo, "config", "user.email", "oops@local.com")
        res = core.check_commit_identity(str(repo), PINS)
        check("no_remote_warns", res is not None and res["level"] == core.IDENTITY_WARN)
    finally:
        sb.close()


def test_commit_check_missing_dir_is_none():
    check("missing_dir_none",
          core.check_commit_identity("C:/definitely/not/here", PINS) is None)


# --- push-time check (unpushed commits) -----------------------------------

def test_push_check_flags_only_unpushed_wrong_commits():
    sb = Sandbox()
    try:
        repo = sb.repo("push", commits=1)
        sb.bare_behind(repo)
        check("push_nothing_unpushed", core.check_push_identity(str(repo), PINS) is None)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "wrong", ident=PERSONAL)
        (repo / "y.txt").write_text("y")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "right")
        res = core.check_push_identity(str(repo), PINS)
        check("push_blocks", res is not None and res["level"] == core.IDENTITY_BLOCK)
        check("push_flags_one", res and len(res["commits"]) == 1)
        check("push_flags_wrong_one",
              res and res["commits"][0]["committer_email"] == PERSONAL[1])
        check("push_fix_rebases", res and "git rebase -r" in res["fix"]
              and "--reset-author" in res["fix"])
    finally:
        sb.close()


def test_push_check_head_only_suggests_amend():
    sb = Sandbox()
    try:
        repo = sb.repo("amend", commits=1)
        sb.bare_behind(repo)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "wrong", ident=PERSONAL)
        res = core.check_push_identity(str(repo), PINS)
        check("amend_fix", res and res["fix"].strip().endswith(
            "git commit --amend --no-edit --reset-author"))
    finally:
        sb.close()


def test_push_check_colleague_author_not_flagged():
    """A rebased/cherry-picked colleague commit: author theirs, committer you."""
    sb = Sandbox()
    try:
        repo = sb.repo("colleague", commits=1)
        sb.bare_behind(repo)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "theirs",
            f"--author={COLLEAGUE[0]} <{COLLEAGUE[1]}>")
        check("colleague_author_ok", core.check_push_identity(str(repo), PINS) is None)
    finally:
        sb.close()


def test_push_check_amended_wrong_author_flagged():
    """Committed as personal, fixed config, amended: author is still personal."""
    sb = Sandbox()
    try:
        repo = sb.repo("amended", commits=1)
        sb.bare_behind(repo)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "oops", ident=PERSONAL)
        git(repo, "commit", "-q", "--amend", "--no-edit")  # committer now WORK
        res = core.check_push_identity(str(repo), PINS)
        check("amended_author_flagged", res is not None and len(res["commits"]) == 1)
        check("amended_author_is_personal",
              res and res["commits"][0]["author_email"] == PERSONAL[1])
    finally:
        sb.close()


def test_push_check_host_override_for_new_remote():
    """Create-Remote: no origin yet, whole history goes to the pinned host."""
    sb = Sandbox()
    try:
        repo = sb.repo("fresh", url=None, commits=2)  # personal identity
        check("no_origin_no_host_none",
              core.check_push_identity(str(repo), PINS) is None)
        res = core.check_push_identity(str(repo), PINS, host=HOST)
        check("override_host_blocks",
              res is not None and res["level"] == core.IDENTITY_BLOCK)
        check("override_host_all_history", res and len(res["commits"]) == 2)
        check("override_host_root_fix", res and "--root" in res["fix"])
    finally:
        sb.close()


def test_push_check_other_host_warns():
    sb = Sandbox()
    try:
        url = "https://github.com/o/r.git"
        repo = sb.repo("ghpush", url=url, commits=1)
        sb.bare_behind(repo, url=url)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "w", ident=("Z", "z@elsewhere.com"))
        res = core.check_push_identity(str(repo), PINS)
        check("push_other_host_warns",
              res is not None and res["level"] == core.IDENTITY_WARN)
    finally:
        sb.close()


def test_push_check_unborn_is_none():
    sb = Sandbox()
    try:
        repo = sb.repo("unborn", commits=0)
        check("unborn_none", core.check_push_identity(str(repo), PINS) is None)
    finally:
        sb.close()


# --- GUI gates (real repos, real commit/push) ------------------------------

def _gui(repo, pins=PINS, enabled=True):
    key = str(repo)
    rs = m.RepoState(path=Path(repo), name=Path(repo).name,
                     folder_name=Path(repo).name, entries=[("??", "z.txt")])
    m.app = SimpleNamespace(repos={key: rs}, non_git_folders={},
                            identity_pins=pins, identity_guard_enabled=enabled)
    posted = []
    m.ui_queue = SimpleNamespace(put=lambda item: posted.append(item))
    return key, posted


def _identity_msgs(posted):
    return [p for p in posted if p[0] == "identity_check"]


def test_gui_commit_blocked_leaves_head():
    sb = Sandbox()
    try:
        repo = sb.repo("guiblock")
        sb.bare_behind(repo)
        git(repo, "config", "user.email", "oops@local.com")
        head = git(repo, "rev-parse", "HEAD")
        (repo / "z.txt").write_text("z")
        key, posted = _gui(repo)
        m.bg_commit_and_push(key, "msg")
        check("gui_block_head_unchanged", git(repo, "rev-parse", "HEAD") == head)
        msgs = _identity_msgs(posted)
        check("gui_block_posted", len(msgs) == 1
              and msgs[0][2][0]["level"] == core.IDENTITY_BLOCK)
        check("gui_block_no_commit_result",
              not any(p[0] == "commit_result" for p in posted))
    finally:
        sb.close()


def test_gui_commit_guard_disabled_commits():
    sb = Sandbox()
    try:
        repo = sb.repo("guioff")
        sb.bare_behind(repo)
        git(repo, "config", "user.email", "oops@local.com")
        head = git(repo, "rev-parse", "HEAD")
        (repo / "z.txt").write_text("z")
        key, posted = _gui(repo, enabled=False)
        m.bg_commit_and_push(key, "msg")
        check("gui_off_committed", git(repo, "rev-parse", "HEAD") != head)
        check("gui_off_no_identity_msg", _identity_msgs(posted) == [])
    finally:
        sb.close()


def test_gui_commit_warn_still_commits_and_pushes():
    sb = Sandbox()
    try:
        url = "https://github.com/o/r.git"
        repo = sb.repo("guiwarn", url=url)
        bare = sb.bare_behind(repo, url=url)
        git(repo, "config", "user.email", "oops@local.com")
        (repo / "z.txt").write_text("z")
        key, posted = _gui(repo)
        m.bg_commit_and_push(key, "msg")
        msgs = _identity_msgs(posted)
        check("gui_warn_posted", len(msgs) == 1
              and msgs[0][2][0]["level"] == core.IDENTITY_WARN)
        results = [p for p in posted if p[0] == "commit_result"]
        check("gui_warn_committed_and_pushed",
              len(results) == 1 and results[0][2] and results[0][3])
        check("gui_warn_reached_remote",
              git(bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD"))
    finally:
        sb.close()


def test_gui_commit_blocked_by_older_unpushed_commit():
    """Commit identity is fine, but an earlier terminal commit is not."""
    sb = Sandbox()
    try:
        repo = sb.repo("guiolder")
        sb.bare_behind(repo)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "terminal", ident=PERSONAL)
        head = git(repo, "rev-parse", "HEAD")
        (repo / "z.txt").write_text("z")
        key, posted = _gui(repo)
        m.bg_commit_and_push(key, "msg")
        check("gui_older_head_unchanged", git(repo, "rev-parse", "HEAD") == head)
        msgs = _identity_msgs(posted)
        check("gui_older_push_check_blocked",
              len(msgs) == 1 and any(r["kind"] == "push" for r in msgs[0][2]))
    finally:
        sb.close()


def test_gui_push_only_blocked_does_not_push():
    sb = Sandbox()
    try:
        repo = sb.repo("guipush")
        bare = sb.bare_behind(repo)
        before = git(bare, "rev-parse", "main")
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "terminal", ident=PERSONAL)
        key, posted = _gui(repo)
        m.bg_push_only(key)
        check("gui_push_blocked_remote_unchanged",
              git(bare, "rev-parse", "main") == before)
        check("gui_push_blocked_posted", len(_identity_msgs(posted)) == 1)
        check("gui_push_blocked_no_push_result",
              not any(p[0] == "push_upstream_result" for p in posted))
    finally:
        sb.close()


def test_gui_push_only_clean_pushes():
    sb = Sandbox()
    try:
        repo = sb.repo("guipushok")
        bare = sb.bare_behind(repo)
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "fine")
        key, posted = _gui(repo)
        m.bg_push_only(key)
        check("gui_push_ok_reached_remote",
              git(bare, "rev-parse", "main") == git(repo, "rev-parse", "HEAD"))
        check("gui_push_ok_no_identity_msg", _identity_msgs(posted) == [])
    finally:
        sb.close()


def test_gui_set_upstream_blocked():
    sb = Sandbox()
    try:
        repo = sb.repo("guiup")
        bare = sb.bare_behind(repo)
        git(repo, "switch", "-q", "-c", "feature")
        (repo / "x.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "terminal", ident=PERSONAL)
        key, posted = _gui(repo)
        m.bg_push_set_upstream(key, "feature")
        has_branch = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "feature"], cwd=str(bare),
            capture_output=True).returncode == 0
        check("gui_upstream_blocked_no_branch", not has_branch)
        check("gui_upstream_blocked_posted", len(_identity_msgs(posted)) == 1)
    finally:
        sb.close()


def test_gui_create_gitlab_remote_blocked_keeps_no_origin():
    sb = Sandbox()
    try:
        repo = sb.repo("guicreate", url=None, commits=1)  # personal history
        key, posted = _gui(repo)
        m.bg_create_gitlab_remote(key, WORK_URL)
        has_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(repo),
            capture_output=True).returncode == 0
        check("gui_create_blocked_no_origin", not has_origin)
        check("gui_create_blocked_posted", len(_identity_msgs(posted)) == 1)
        results = [p for p in posted if p[0] == "create_remote_result"]
        check("gui_create_blocked_result_failed",
              len(results) == 1 and results[0][2] is False)
    finally:
        sb.close()


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # a crash is a failure, not a skip
                check(f"{name} raised {type(exc).__name__}: {exc}", False)
    if _failures:
        print(f"\n{len(_failures)} test(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
