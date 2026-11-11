"""Identity pins are written into git config by the app -- no hand editing.

Every case runs the real ``git`` binary. The global config is a temp file
(``GIT_CONFIG_GLOBAL``) and the managed hub file lives in a temp dir, so the
user's own ~/.gitconfig is never read or written. "Does it work" is always
answered by asking git what a real repo resolves ``user.email`` to.

Run: python tests/test_identity_sync.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import ai_commit_core as core

_failures = []

HOST = "gitlab.example.com"
WORK = {"host": HOST, "name": "Work Name", "email": "work@corp.com",
        "login": "jdoe"}
PERSONAL = ("Personal Name", "personal@home.com")


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def git(cwd, *args):
    res = subprocess.run(["git"] + list(args), cwd=str(cwd),
                         capture_output=True, text=True)
    return res.returncode, res.stdout.strip()


class Sandbox:
    def __init__(self, extra_global=""):
        self.root = Path(tempfile.mkdtemp(prefix="ident-sync-"))
        self.global_cfg = self.root / "global.cfg"
        self.hub = self.root / "managed" / "git-identities.gitconfig"
        self.global_cfg.write_text(
            f"[user]\n\tname = {PERSONAL[0]}\n\temail = {PERSONAL[1]}\n"
            "[init]\n\tdefaultBranch = main\n" + extra_global)
        self._old = {k: os.environ.get(k)
                     for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = str(self.global_cfg)
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self._n = 0

    def sync(self, pins):
        return core.sync_identity_config(pins, hub_path=self.hub)

    def resolves(self, url):
        """(name, email) git resolves in a fresh repo whose origin is *url*."""
        self._n += 1
        path = self.root / f"r{self._n}"
        git(self.root, "init", "-q", str(path))
        if url:
            git(path, "remote", "add", "origin", url)
        return git(path, "config", "user.name")[1], git(path, "config", "user.email")[1]

    def global_get(self, key):
        return git(self.root, "config", "--global", "--get", key)[1]

    def close(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)


def test_applies_to_every_url_form():
    sb = Sandbox()
    try:
        result = sb.sync([WORK])
        check("sync_ok", result["ok"] and result["hosts"].get(HOST) == "ok")
        for url in (f"https://{HOST}/grp/proj.git",
                    f"https://jdoe@{HOST}/grp/proj.git",
                    f"https://{HOST}:8443/grp/proj.git",
                    f"git@{HOST}:grp/proj.git",
                    f"git@{HOST}:grp/sub/proj.git",
                    f"ssh://git@{HOST}/grp/proj.git",
                    f"ssh://git@{HOST}:2222/grp/proj.git"):
            check(f"pinned_identity <- {url}",
                  sb.resolves(url) == (WORK["name"], WORK["email"]))
        check("other_host_keeps_global",
              sb.resolves("https://github.com/o/r.git") == PERSONAL)
        check("lookalike_host_keeps_global",
              sb.resolves(f"https://{HOST}.evil.com/o/r.git") == PERSONAL)
        check("no_remote_keeps_global", sb.resolves("") == PERSONAL)
        check("credential_username_set",
              sb.global_get(f"credential.https://{HOST}.username") == "jdoe")
    finally:
        sb.close()


def test_guard_passes_without_manual_config():
    sb = Sandbox()
    try:
        sb.sync([WORK])
        sb.resolves(f"https://{HOST}/grp/proj.git")
        repo = sb.root / f"r{sb._n}"
        check("guard_ok_after_sync",
              core.check_commit_identity(str(repo), [WORK]) is None)
    finally:
        sb.close()


def test_idempotent():
    sb = Sandbox()
    try:
        sb.sync([WORK])
        before = sb.global_cfg.read_bytes()
        hub_before = sb.hub.read_bytes()
        result = sb.sync([WORK])
        check("second_sync_ok", result["ok"])
        check("global_unchanged", sb.global_cfg.read_bytes() == before)
        check("hub_unchanged", sb.hub.read_bytes() == hub_before)
        text = sb.global_cfg.read_text()
        check("single_global_include", text.count(sb.hub.as_posix()) == 1)
    finally:
        sb.close()


def test_edit_and_remove():
    sb = Sandbox()
    try:
        sb.sync([WORK])
        edited = dict(WORK, email="new@corp.com", login="jdoe2")
        sb.sync([edited])
        check("edit_applies", sb.resolves(f"https://{HOST}/g/p.git")
              == (WORK["name"], "new@corp.com"))
        check("edit_login", sb.global_get(f"credential.https://{HOST}.username") == "jdoe2")

        result = sb.sync([])
        check("remove_ok", result["ok"])
        check("remove_restores_global",
              sb.resolves(f"https://{HOST}/g/p.git") == PERSONAL)
        check("remove_unsets_login",
              sb.global_get(f"credential.https://{HOST}.username") == "")
        text = sb.global_cfg.read_text()
        check("remove_drops_global_include", sb.hub.as_posix() not in text)
        check("remove_keeps_user_config",
              PERSONAL[1] in text and "defaultBranch = main" in text)
        leftovers = [p.name for p in sb.hub.parent.glob("*.gitconfig")]
        check("remove_deletes_managed_files", leftovers == [])
    finally:
        sb.close()


def test_users_own_login_for_other_host_untouched():
    sb = Sandbox('[credential "https://github.com"]\n\tusername = mine\n')
    try:
        sb.sync([WORK])
        sb.sync([])
        check("other_login_kept",
              sb.global_get("credential.https://github.com.username") == "mine")
    finally:
        sb.close()


def test_preexisting_login_for_pinned_host_restored():
    """A login the user had set before pinning comes back when the pin goes."""
    sb = Sandbox(f'[credential "https://{HOST}"]\n\tusername = before\n')
    try:
        sb.sync([WORK])
        check("login_overridden", sb.global_get(f"credential.https://{HOST}.username") == "jdoe")
        sb.sync([])
        check("login_restored", sb.global_get(f"credential.https://{HOST}.username") == "before")
    finally:
        sb.close()


def test_pin_without_login_leaves_credential_alone():
    sb = Sandbox(f'[credential "https://{HOST}"]\n\tusername = mine\n')
    try:
        sb.sync([dict(WORK, login="")])
        check("no_login_untouched",
              sb.global_get(f"credential.https://{HOST}.username") == "mine")
        check("no_login_identity_applied",
              sb.resolves(f"https://{HOST}/g/p.git") == (WORK["name"], WORK["email"]))
    finally:
        sb.close()


def test_later_user_section_is_repaired():
    """A [user] block appended after ours would win -- sync moves ours last."""
    sb = Sandbox()
    try:
        sb.sync([WORK])
        with open(sb.global_cfg, "a", encoding="utf-8") as fh:
            fh.write("[user]\n\temail = late@home.com\n")
        check("late_user_wins_before_repair",
              sb.resolves(f"https://{HOST}/g/p.git")[1] == "late@home.com")
        result = sb.sync([WORK])
        check("repair_ok", result["ok"])
        check("repaired", sb.resolves(f"https://{HOST}/g/p.git")[1] == WORK["email"])
        check("repair_keeps_late_global",
              sb.resolves("https://github.com/o/r.git")[1] == "late@home.com")
    finally:
        sb.close()


def test_pin_without_email_is_reported_not_applied():
    sb = Sandbox()
    try:
        result = sb.sync([dict(WORK, email="")])
        check("no_email_not_ok", result["hosts"].get(HOST, "ok") != "ok")
        check("no_email_global_used",
              sb.resolves(f"https://{HOST}/g/p.git") == PERSONAL)
    finally:
        sb.close()


def test_special_characters_round_trip():
    sb = Sandbox()
    try:
        odd = dict(WORK, name='Jo "JJ" O\'Neil #1; x\\y')
        sb.sync([odd])
        check("odd_name_round_trip",
              sb.resolves(f"https://{HOST}/g/p.git")[0] == odd["name"])
    finally:
        sb.close()


def test_two_hosts():
    sb = Sandbox()
    try:
        other = {"host": "git.other.org", "name": "Other", "email": "o@other.org",
                 "login": ""}
        result = sb.sync([WORK, other])
        check("two_ok", result["ok"])
        check("two_first", sb.resolves(f"https://{HOST}/g/p.git")[1] == WORK["email"])
        check("two_second", sb.resolves("https://git.other.org/g/p.git")[1] == "o@other.org")
        sb.sync([other])
        check("two_drop_first", sb.resolves(f"https://{HOST}/g/p.git") == PERSONAL)
        check("two_keep_second", sb.resolves("https://git.other.org/g/p.git")[1] == "o@other.org")
    finally:
        sb.close()


def test_normalize_keeps_login():
    pins = core.normalize_identity_pins([{"host": HOST, "name": "n", "email": "e",
                                          "login": " jdoe "}])
    check("normalize_login", pins[0]["login"] == "jdoe")
    pins = core.normalize_identity_pins([{"host": HOST, "name": "n", "email": "e"}])
    check("normalize_login_default", pins[0]["login"] == "")


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                check(f"{name} raised {type(exc).__name__}: {exc}", False)
    if _failures:
        print(f"\n{len(_failures)} check(s) failed.")
        sys.exit(1)
    print("All tests passed.")


if __name__ == "__main__":
    main()
