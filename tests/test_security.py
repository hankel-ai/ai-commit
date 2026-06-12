"""Tests for the security hardening: credential redaction in the activity log,
sensitive-filename detection, and secret/symlink handling in get_diff.

Headless: imports only activity_log + ai_commit_core (no Dear PyGui).
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import activity_log
import ai_commit_core


# ---------------------------------------------------------------------------
# redact_credentials
# ---------------------------------------------------------------------------

def test_redact_pat_url():
    out = activity_log.redact_credentials(
        "fatal: unable to access 'https://ghp_abc123XYZ@github.com/o/r.git/'"
    )
    assert "ghp_abc123XYZ" not in out
    assert "https://***@github.com/o/r.git" in out


def test_redact_user_pass_url():
    out = activity_log.redact_credentials("push to https://user:s3cret@host/repo")
    assert "s3cret" not in out
    assert "https://***@host/repo" in out


def test_redact_leaves_clean_text_alone():
    msg = "git push origin main\nhttps://github.com/o/r.git"
    assert activity_log.redact_credentials(msg) == msg


def test_redact_handles_none_and_empty():
    assert activity_log.redact_credentials(None) is None
    assert activity_log.redact_credentials("") == ""


def test_log_git_redacts_stderr_detail():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    activity_log.LOG_PATH = path
    activity_log._buffer.clear()
    activity_log.set_enabled(True)
    activity_log.clear_file()

    activity_log.log_git(
        ["push"], cwd="/tmp/repo", rc=1,
        stderr="fatal: could not read from 'https://ghp_tok123@github.com/o/r.git'",
    )
    entry = activity_log.get_buffer()[-1]
    assert "ghp_tok123" not in entry["detail"]
    # And it never reaches the on-disk file either.
    raw = open(path, encoding="utf-8").read()
    assert "ghp_tok123" not in raw


# ---------------------------------------------------------------------------
# is_sensitive_filename
# ---------------------------------------------------------------------------

def test_sensitive_filenames_match():
    for name in (".env", ".env.production", "server.pem", "tls.key",
                 "id_rsa", "id_ed25519.pub", "credentials", "credentials.json",
                 "client_secret.json", "terraform.tfvars", "sub/dir/.env",
                 ".npmrc", "kubeconfig", "prod.kubeconfig", "MySecrets.txt"):
        assert ai_commit_core.is_sensitive_filename(name), name


def test_normal_filenames_do_not_match():
    for name in ("main.py", "README.md", "envelope.txt", "keyboard.js",
                 "src/app.ts", "environment.md"):
        assert not ai_commit_core.is_sensitive_filename(name), name


# ---------------------------------------------------------------------------
# get_diff secret/symlink handling (uses a real throwaway git repo)
# ---------------------------------------------------------------------------

def _make_repo():
    repo = tempfile.mkdtemp(prefix="acg_sec_test_")
    subprocess.run(["git", "init", "-q", repo], check=True)
    return repo


def test_get_diff_withholds_sensitive_untracked_content():
    repo = _make_repo()
    try:
        with open(os.path.join(repo, ".env"), "w", encoding="utf-8") as fh:
            fh.write("API_KEY=supersecretvalue\n")
        with open(os.path.join(repo, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("print('hello')\n")
        diff = ai_commit_core.get_diff(repo)
        assert "supersecretvalue" not in diff
        assert "content withheld" in diff
        assert ".env" in diff            # file name still visible to the LLM
        assert "print('hello')" in diff  # normal files still included
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def test_get_diff_skips_symlinks():
    repo = _make_repo()
    try:
        target = os.path.join(repo, "outside.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("LINKTARGETCONTENT\n")
        link = os.path.join(repo, "link.txt")
        try:
            os.symlink(target, link)
        except OSError:
            print("skip  test_get_diff_skips_symlinks (no symlink privilege)")
            return
        diff = ai_commit_core.get_diff(repo)
        # The link's resolved content must not be attributed to the link path.
        assert "+++ b/link.txt\n(new symlink" in diff
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
