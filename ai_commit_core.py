"""Shared logic for AI commit message generation -- used by both CLI and GUI."""

import fnmatch
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


SYSTEM_PROMPT = (
    "You are a git commit message generator. Write a concise commit message "
    "for the following changes. Use conventional commit format:\n"
    "- First line: type(scope): description (max 72 chars)\n"
    "- Types: feat, fix, refactor, docs, style, test, chore, build\n"
    "- Optional body after blank line for non-trivial changes\n"
    "Return ONLY the commit message, no extra commentary."
)

MAX_DIFF_CHARS = 8000

# Untracked files whose basename matches any of these patterns have their
# content withheld from the LLM prompt -- the diff sent to the AI provider may
# leave the machine (e.g. Ollama cloud models), and files like these tend to
# hold credentials that aren't gitignored yet.
SENSITIVE_FILE_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.jks",
    "*.keystore", "*.kdbx", "*.tfvars", "id_rsa*", "id_ed25519*", "id_ecdsa*",
    "credentials", "credentials.*", "*secret*", ".netrc", ".npmrc", ".pypirc",
    "*.htpasswd", "kubeconfig", "*.kubeconfig",
)


def is_sensitive_filename(filepath):
    """True if the file's basename matches a known secret-bearing pattern."""
    name = Path(filepath).name.lower()
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_FILE_PATTERNS)

STATUS_LABELS = {
    "M": "modified", "A": "added", "D": "deleted", "R": "renamed",
    "C": "copied", "??": "untracked", "MM": "modified", "AM": "added",
    "UU": "conflict",
}


class OllamaError(Exception):
    """Raised when Ollama API call fails."""
    pass


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

# Optional callback invoked after every git command, for the activity log.
# Signature: fn(args, cwd, rc, duration_ms, stderr). Left unset by default so the
# core stays dependency-light and unit-testable without the GUI; the GUI installs
# it at startup via activity_log.install_git_logger().
_GIT_LOGGER = None


def set_git_logger(fn):
    """Register (or clear, with None) the per-command git logging callback."""
    global _GIT_LOGGER
    _GIT_LOGGER = fn


# Per-repo serialization. The GUI runs git on a thread pool plus a polling loop,
# so two commands can hit the same repo at once -- one then fails on `index.lock`
# and (via get_status returning [] on error) can be misread as a clean tree,
# bypassing the branch-switch confirm gate. A lock per repo dir guarantees only
# one git command runs in a given repo at a time. Locks are leaf-level (run_git
# never calls run_git), so a plain Lock can't deadlock.
_REPO_LOCKS = {}
_REPO_LOCKS_GUARD = threading.Lock()


def _repo_lock(cwd):
    key = os.path.normcase(os.path.abspath(str(cwd))) if cwd else ""
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[key] = lock
        return lock


def _run_git(args, cwd, binary=False):
    """Shared runner for :func:`run_git` / :func:`run_git_bytes`."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    if not binary:
        kwargs.update(text=True, encoding="utf-8", errors="replace")
    start = time.perf_counter()
    with _repo_lock(cwd):
        result = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, **kwargs
        )
    stderr = result.stderr
    if binary:
        stderr = stderr.decode("utf-8", "replace")
    if _GIT_LOGGER is not None:
        try:
            _GIT_LOGGER(
                args, cwd, result.returncode,
                int((time.perf_counter() - start) * 1000),
                stderr,
            )
        except Exception:
            pass
    return result.returncode, result.stdout, stderr


def run_git(args, cwd):
    """Run a git command and return (returncode, stdout, stderr).

    Serialized per repo dir (see ``_repo_lock``) so concurrent app threads can't
    collide on the same repo's index.
    """
    return _run_git(args, cwd)


def run_git_bytes(args, cwd):
    """Like :func:`run_git` but stdout stays raw ``bytes`` (stderr is decoded).

    Text mode applies universal-newline translation, which rewrites CRLF to LF
    in the captured output -- fatal when the *point* of the call is to inspect
    line endings (see :func:`is_eol_only_change`).
    """
    return _run_git(args, cwd, binary=True)


def is_git_repo(path):
    """Return True only if *path* is the root of a git repository."""
    rc, stdout, _ = run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if rc != 0:
        return False
    return Path(stdout.strip()).resolve() == Path(path).resolve()


# Git refuses to run in a repo whose worktree owner isn't the current user
# (the safe.directory hardening). On Windows a folder created by an *elevated*
# process is owned by BUILTIN\Administrators (SID S-1-5-32-544) even though its
# contents are owned by the user, so every git command in it exits 128 --
# including the rev-parse behind :func:`is_git_repo`. That is why such a folder
# keeps presenting as "not a git repo" no matter how often it is Init'd.
_DUBIOUS_OWNERSHIP_MARKERS = ("detected dubious ownership", "unsafe repository")

# The well-known owners worth naming; anything else is shown as a bare SID.
_KNOWN_SIDS = {
    "S-1-5-32-544": "BUILTIN\\Administrators",
    "S-1-5-18": "NT AUTHORITY\\SYSTEM",
}


def is_dubious_ownership(text):
    """True if git refused a command because of the safe.directory check."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _DUBIOUS_OWNERSHIP_MARKERS)


def _owner_from_stderr(stderr):
    """Extract the owner git named in its refusal, or "" if it didn't say.

    Git prints the owner on the line after ``is owned by:``, quoted and
    tab-indented. The POSIX build says "is owned by someone else" instead and
    names nobody.
    """
    lines = (stderr or "").splitlines()
    for i, line in enumerate(lines):
        if line.rstrip().endswith("is owned by:"):
            for follow in lines[i + 1:]:
                owner = follow.strip().strip("'\"")
                if owner:
                    return owner
            break
    return ""


def describe_dubious_ownership(path, stderr=""):
    """Explain -- actionably -- why git won't touch the repo at *path*.

    Replaces git's five-line fatal with the two fixes that actually apply:
    correct the owner (preferred -- keeps the safe.directory check meaningful)
    or add a permanent exception.
    """
    owner = _owner_from_stderr(stderr)
    owner_note = ""
    if owner:
        friendly = _KNOWN_SIDS.get(owner)
        owner_note = f" (owner {owner}{' = ' + friendly if friendly else ''})"
    safe_path = str(path).replace("\\", "/")
    return "\n".join([
        f"Git refuses to use this folder: its owner isn't your user "
        f"account{owner_note}.",
        "A folder created by an elevated process gets this owner, and every git",
        "command inside it then fails -- so Init appears to do nothing.",
        "Fix the owner (preferred):",
        f'  icacls "{path}" /setowner "%USERDOMAIN%\\%USERNAME%"',
        "or trust it as-is:",
        f"  git config --global --add safe.directory {safe_path}",
    ])


def verify_repo_usable(cwd):
    """Return ``(ok, detail)``: can git actually operate inside *cwd*?

    ``git init`` succeeds in places git will subsequently refuse to read, so a
    zero exit from init is not proof of a working repo -- callers must verify
    and surface *detail* rather than let the folder silently revert to
    "not a git repo".
    """
    rc, stdout, stderr = run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc == 0:
        return True, ""
    if is_dubious_ownership(stderr):
        return False, describe_dubious_ownership(cwd, stderr)
    first = next((ln.strip() for ln in (stderr or stdout).splitlines() if ln.strip()),
                 "")
    return False, first or "git could not read the repository"


def _unquote_path(p):
    """Strip the quotes git adds around paths containing spaces/specials."""
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        return p[1:-1]
    return p


def read_status(cwd):
    """Run ``git status --porcelain`` and return ``(ok, entries)``.

    ``ok`` is False when git itself failed (non-zero exit -- e.g. a concurrent op
    held ``index.lock``), which is NOT the same as a clean tree. Callers that
    make destructive decisions (branch switch) must treat ``ok=False`` as
    "unknown" and refuse to proceed, never as "clean".
    """
    rc, stdout, _ = run_git(["status", "--porcelain"], cwd=cwd)
    if rc != 0:
        return False, []
    entries = []
    for line in stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2].strip()
        filepath = _unquote_path(line[3:])
        entries.append((code, filepath))
    return True, entries


def get_status(cwd):
    """Return list of (status_code, filepath) tuples from git status --porcelain.

    Returns ``[]`` on git failure (indistinguishable from a clean tree). Callers
    that must tell those two apart should use :func:`read_status` instead.
    """
    _ok, entries = read_status(cwd)
    return entries


def parse_branch_header(line):
    """Parse the ``## ...`` header line of ``git status --porcelain --branch``.

    Returns a dict ``{branch, ahead, behind, classification, detached}`` where
    ``classification`` is ``""`` (upstream live), ``"local only"`` (no upstream)
    or ``"stale"`` (upstream gone). This folds what previously took three
    separate git calls (``get_current_branch``, ``get_sync_status`` local part,
    ``get_branch_classification``) into pure parsing of one already-fetched line.

    Porcelain output is guaranteed non-localized and stable, so the marker
    strings (``[ahead N, behind M]``, ``[gone]``, ``No commits yet on``) are
    safe to match. Refnames cannot contain ``..`` so ``...`` is an unambiguous
    branch/upstream separator. See tests/test_status_branch.py for every shape.
    """
    info = {"branch": "", "ahead": 0, "behind": 0,
            "classification": "", "detached": False}
    if not line.startswith("## "):
        return info
    body = line[3:].strip()

    # Detached HEAD: "## HEAD (no branch)"
    if body.startswith("HEAD (no branch)"):
        info["branch"] = "HEAD"
        info["detached"] = True
        return info

    # Unborn branch (fresh repo, no commits): "## No commits yet on <branch>"
    m = re.match(r"No commits yet on (.+)$", body)
    if m:
        info["branch"] = m.group(1).strip()
        return info

    # Optional trailing bracket: "[ahead N, behind M]" / "[ahead N]" /
    # "[behind M]" / "[gone]"
    bracket = ""
    mb = re.search(r"\s\[(.+)\]$", body)
    if mb:
        bracket = mb.group(1).strip()
        body = body[:mb.start()]

    if "..." in body:
        branch, _sep, _upstream = body.partition("...")
        info["branch"] = branch.strip()
        if bracket == "gone":
            info["classification"] = "stale"
        else:
            ma = re.search(r"ahead (\d+)", bracket)
            mbh = re.search(r"behind (\d+)", bracket)
            if ma:
                info["ahead"] = int(ma.group(1))
            if mbh:
                info["behind"] = int(mbh.group(1))
    else:
        # No "..." => branch has no upstream tracking ref.
        info["branch"] = body.strip()
        info["classification"] = "local only"
    return info


def read_status_branch(cwd):
    """Run ``git status --porcelain --branch`` once; return (ok, entries, info).

    ``ok`` is False when git itself failed (non-zero exit) -- same contract as
    :func:`read_status` (a failure is NOT a clean tree). ``entries`` is the same
    ``(code, filepath)`` list as :func:`get_status`; ``info`` is the parsed
    branch header (see :func:`parse_branch_header`).

    This is the folded poll path: one spawn yields dirty state, current branch,
    ahead/behind, and branch classification together. Note: it does NOT fetch --
    ahead/behind is measured against the local remote-tracking ref, identical to
    ``get_sync_status(fetch=False)``. Callers wanting fresh counts must call
    :func:`fetch_remote` first.
    """
    rc, stdout, _ = run_git(["status", "--porcelain", "--branch"], cwd=cwd)
    if rc != 0:
        return False, [], parse_branch_header("")
    info = parse_branch_header("")
    entries = []
    for line in stdout.splitlines():
        if line.startswith("## "):
            info = parse_branch_header(line)
            continue
        if len(line) < 4:
            continue
        code = line[:2].strip()
        filepath = _unquote_path(line[3:])
        entries.append((code, filepath))
    return True, entries, info


def fetch_remote(cwd):
    """Fetch from origin silently, pruning stale remote-tracking refs.

    Errors (offline, no remote) are ignored. Split out of get_sync_status so the
    folded poll path can fetch once, then read fresh ahead/behind from the
    status --branch header.
    """
    run_git(["fetch", "--prune", "--quiet"], cwd=cwd)


def get_git_global_user():
    """Return (global_name, global_email) from ``git config --global``.

    Read once at GUI startup for the toolbar identity label -- not per repo,
    so it doesn't flood the activity log.
    """
    rc_n, name, _ = run_git(["config", "--global", "user.name"], cwd=".")
    rc_e, email, _ = run_git(["config", "--global", "user.email"], cwd=".")
    global_name = name.strip() if rc_n == 0 else ""
    global_email = email.strip() if rc_e == 0 else ""
    return global_name, global_email


def get_active_github_account():
    """Return the login name of the currently active gh CLI user, or ''."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, **kwargs,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def get_repo_visibility(cwd):
    """Return 'private' or 'public' for the repo, or '' if unknown."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "visibility", "--jq", ".visibility"],
            cwd=cwd,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=10, **kwargs,
        )
        if result.returncode == 0:
            return result.stdout.strip().upper()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def get_github_account(remote_url):
    """Extract the GitHub owner/account name from a remote URL.

    E.g. 'https://github.com/hankel-ai/repo.git' -> 'hankel-ai'
    Also handles PAT-embedded URLs and SSH URLs.
    """
    if not remote_url:
        return ""
    url = remote_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # SSH: git@github.com:owner/repo
    if ":" in url and url.startswith("git@"):
        path = url.split(":", 1)[1]
        parts = path.split("/")
        return parts[0] if parts else ""
    # HTTPS (possibly with PAT): https://[token@]github.com/owner/repo
    # Strip scheme
    if "://" in url:
        url = url.split("://", 1)[1]
    # Strip optional token@ prefix
    if "@" in url:
        url = url.split("@", 1)[1]
    # Now: github.com/owner/repo
    parts = url.split("/")
    return parts[1] if len(parts) >= 2 else ""


def get_remote_url(cwd):
    """Return the HTTPS URL for the repo's origin remote, or empty string."""
    rc, stdout, _ = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if rc != 0 or not stdout.strip():
        return ""
    url = stdout.strip()
    # Convert SSH URLs to HTTPS
    if url.startswith("git@"):
        # git@github.com:user/repo.git -> https://github.com/user/repo
        url = url.replace(":", "/", 1).replace("git@", "https://", 1)
    # Strip trailing .git
    if url.endswith(".git"):
        url = url[:-4]
    return url


def get_head_sha(cwd):
    """Return the full SHA of HEAD, or empty string."""
    rc, stdout, _ = run_git(["rev-parse", "HEAD"], cwd=cwd)
    return stdout.strip() if rc == 0 else ""


def get_current_branch(cwd):
    """Return the current branch name, or empty string."""
    rc, stdout, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return stdout.strip() if rc == 0 else ""


def find_autostash_ref(stash_list_output, branch):
    """Return the topmost ``stash@{N}`` ref tagged for *branch*, or None.

    Matches only stashes created by ai-commit's Switch Branch autostash
    (message ``ai-commit-autostash:<branch>``); never manual ``git stash``
    entries. ``git stash list`` orders newest first, so the first match is the
    most recent.
    """
    if not branch or branch == "HEAD":
        return None
    marker = f"ai-commit-autostash:{branch}"
    for line in stash_list_output.splitlines():
        if line.rstrip().endswith(marker):
            return line.split(":", 1)[0].strip()
    return None


def compute_header_open(*, override, paused, has_activity, force_expand,
                        preserve_open, has_prior, prior_open,
                        force_collapse=False):
    """Decide whether a repo's collapsing header should be open on (re)build.

    Pure decision logic extracted from the GUI so it can be unit-tested.

    ``preserve_open`` is True for *partial* rebuilds (e.g. single-repo refresh,
    refresh-then-generate) where the user's current expand/collapse state must
    survive the rebuild -- repos must NOT be auto-collapsed. It is False for
    *full* refreshes (the automatic poll loop or a manual Refresh-all), which
    are the only cases where the activity-based default is (re)applied.

    ``force_collapse`` is the per-repo opt-out of ``preserve_open`` (mirror of
    ``force_expand``): set after a successful Commit & Push so the just-pushed
    repo re-applies the activity-based default (and collapses, having no more
    activity) on the very next partial rebuild, while every *other* repo keeps
    its open state.

    Precedence:
      1. force-pause override → always collapsed (explicit user setting)
      2. globally paused (unless force-active or force-expand) → collapsed
      3. preserve_open with a known prior state → keep the prior state
         (unless force_expand or force_collapse re-applies the activity default)
      4. otherwise → open iff the repo has activity
    """
    if override == "pause":
        return False
    if paused and override != "active" and not force_expand:
        return False
    if preserve_open and not force_expand and not force_collapse and has_prior:
        return prior_open
    return has_activity


def parse_commit_date(date_str):
    """Convert a git ``%ci`` timestamp to ``(short_date, commit_ts)`` in local time.

    ``%ci`` is written in the *committer's* timezone with an explicit offset
    (``2026-07-20 21:30:00 +0530``). The offset is honoured and the result
    re-rendered in the *viewer's* zone, so a commit pushed by a colleague in IST
    or by a CI runner on UTC reads as the local wall-clock time it happened at --
    not as a stamp hours in the future. ``commit_ts`` is the absolute epoch, so
    the ``is_repo_active`` recency window is measured against the real instant.

    A ``%ci`` without an offset is assumed local; anything unparseable degrades to
    the leading 16 characters with a ``0.0`` timestamp. Pure/testable -- see
    ``tests/test_commit_date.py``.
    """
    date_str = (date_str or "").strip()
    if not date_str:
        return "", 0.0
    dt = None
    for text, fmt in ((date_str[:25], "%Y-%m-%d %H:%M:%S %z"),
                      (date_str[:19], "%Y-%m-%d %H:%M:%S")):
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return date_str[:16], 0.0
    try:
        if dt.tzinfo is None:      # no offset in %ci -- treat as local
            dt = dt.astimezone()
        local = dt.astimezone()
        short_date = local.strftime("%b %d %I:%M%p").replace("AM", "am").replace("PM", "pm")
        return short_date, dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return date_str[:16], 0.0


def get_last_commit(cwd):
    """Return (full_message, short_date, commit_ts) for HEAD, or ("", "", 0.0).

    One ``git log`` call: ``%ci`` (a fixed single-line date) on the first line,
    then ``%B`` (the free-form, possibly multi-line message). Split on only the
    first newline so the body's own newlines never collide with the date.

    The date is converted to the viewer's local timezone by ``parse_commit_date``.
    """
    rc, out, _ = run_git(["log", "-1", "--format=%ci%n%B"], cwd=cwd)
    if rc != 0:
        return "", "", 0.0
    date_str, _, msg_out = out.partition("\n")
    short_date, commit_ts = parse_commit_date(date_str)
    return msg_out.strip(), short_date, commit_ts


def is_repo_active(commit_ts, dirty, ahead, behind, now, recent_days):
    """Whether a repo counts as *active* (shown in the list + polled every cycle).

    Active when the working tree is dirty, there are unpushed/unpulled commits
    (ahead/behind), or the last commit is within *recent_days*. Otherwise the repo
    is idle: hidden by the recency filter and polled on the slow idle cadence.

    Pure/testable tier decision -- see docs/polling-performance.md and
    tests/test_recency.py.
    """
    if dirty or ahead or behind:
        return True
    if commit_ts and (now - commit_ts) <= recent_days * 86400:
        return True
    return False


def is_folder_recent(mtime, now, recent_days):
    """Whether a non-git folder counts as *recent* (shown by the recency filter).

    Folder counterpart of ``is_repo_active`` for directories with no git signals:
    recent when the folder's modification time is within *recent_days*. An
    unknown mtime (``0.0``, e.g. ``stat`` failed) counts as recent so a folder
    is never silently hidden. Pure/testable -- see tests/test_recency.py.
    """
    if not mtime:
        return True
    return (now - mtime) <= recent_days * 86400


def get_sync_status(cwd, fetch=True):
    """Return (ahead, behind) commit counts vs tracking branch.

    When *fetch* is True (default), fetches from origin first.
    Returns (0, 0) if there is no remote or no tracking branch.
    """
    if fetch:
        # Fetch silently -- ignore errors (offline, no remote, etc.).
        # --prune drops stale remote-tracking refs so branch_status detection
        # ("stale" vs "local only" vs synced) stays accurate.
        run_git(["fetch", "--prune", "--quiet"], cwd=cwd)

    # Find the upstream tracking branch
    rc, upstream, _ = run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd)
    if rc != 0 or not upstream.strip():
        return 0, 0

    # Count commits ahead/behind
    rc, stdout, _ = run_git(["rev-list", "--left-right", "--count", f"HEAD...{upstream.strip()}"], cwd=cwd)
    if rc != 0 or not stdout.strip():
        return 0, 0
    parts = stdout.strip().split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def get_branch_classification(cwd):
    """Return dict of local branch name -> "", "local only", or "stale".

    "local only" -> branch has no upstream configured.
    "stale"      -> branch has an upstream, but the upstream ref no longer
                    exists locally (i.e. remote branch was deleted and the
                    last fetch ran with --prune).
    ""           -> upstream exists; branch is in sync (ahead/behind aside).
    """
    # Skip classification entirely for repos with no remote configured --
    # the header already shows "LOCAL", so per-branch "(local only)" is noise.
    rc, remotes_out, _ = run_git(["remote"], cwd=cwd)
    if rc != 0 or not remotes_out.strip():
        return {}

    rc, stdout, _ = run_git(
        ["for-each-ref", "--format=%(refname:short)|%(upstream:short)", "refs/heads/"],
        cwd=cwd,
    )
    if rc != 0:
        return {}

    rc2, stdout2, _ = run_git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/"],
        cwd=cwd,
    )
    live_remote_refs = {
        line.strip() for line in stdout2.splitlines() if line.strip()
    } if rc2 == 0 else set()

    result = {}
    for line in stdout.splitlines():
        if "|" not in line:
            continue
        name, upstream = line.split("|", 1)
        name = name.strip()
        upstream = upstream.strip()
        if not name:
            continue
        if not upstream:
            result[name] = "local only"
        elif upstream not in live_remote_refs:
            result[name] = "stale"
        else:
            result[name] = ""
    return result


def get_incoming_changes(cwd):
    """Return a summary of commits that a pull would bring in.

    Assumes fetch has already been done (get_sync_status does this).
    Returns (commits_text, diffstat_text) where each is a string.
    commits_text has one line per incoming commit.
    diffstat_text is the --stat output of the diff.
    Returns ("", "") if there is nothing incoming or no upstream.
    """
    # Find upstream
    rc, upstream, _ = run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=cwd
    )
    if rc != 0 or not upstream.strip():
        return "", ""
    upstream = upstream.strip()

    # Incoming commits: HEAD..upstream
    rc, commits_out, _ = run_git(
        ["log", "--oneline", "--no-decorate", f"HEAD..{upstream}"], cwd=cwd
    )
    commits_text = commits_out.strip() if rc == 0 else ""

    # Diffstat: what files would change
    rc, stat_out, _ = run_git(
        ["diff", "--stat", f"HEAD...{upstream}"], cwd=cwd
    )
    diffstat_text = stat_out.strip() if rc == 0 else ""

    return commits_text, diffstat_text


def do_pull(cwd):
    """Run git pull. Returns (success: bool, detail: str)."""
    rc, stdout, stderr = run_git(["pull"], cwd=cwd)
    if rc != 0:
        return False, f"git pull failed: {stderr.strip()}"
    return True, stdout.strip()


def get_diff(cwd):
    """Build a combined diff string: staged + unstaged changes and untracked file contents."""
    parts = []

    # Diff of tracked files against HEAD (staged + unstaged)
    rc, stdout, _ = run_git(["diff", "HEAD"], cwd=cwd)
    if rc != 0:
        # Possibly no commits yet -- try diff of staged files
        _, stdout, _ = run_git(["diff", "--cached"], cwd=cwd)
    if stdout.strip():
        parts.append(stdout)

    # Untracked files -- show their content so the LLM knows what's new.
    # Use ls-files so untracked *directories* are expanded into individual
    # files; `status --porcelain` collapses them to a single "?? dir/" line.
    _, untracked_out, _ = run_git(
        ["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd,
    )
    for filepath in untracked_out.split("\0"):
        if not filepath:
            continue
        full = Path(cwd) / filepath
        # Never follow symlinks -- a link pointing outside the repo would leak
        # its target's content into the prompt.
        if full.is_symlink():
            parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new symlink, content not included)")
            continue
        if full.is_file():
            if is_sensitive_filename(filepath):
                parts.append(
                    f"--- /dev/null\n+++ b/{filepath}\n"
                    "(new file, content withheld - filename matches secret pattern)"
                )
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
                parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new file)\n{content}")
            except OSError:
                parts.append(f"--- /dev/null\n+++ b/{filepath}\n(new file, unreadable)")

    combined = "\n".join(parts)
    if len(combined) > MAX_DIFF_CHARS:
        combined = combined[:MAX_DIFF_CHARS] + "\n\n[truncated -- diff too large]"
    return combined


# ---------------------------------------------------------------------------
# EOL-only changes: dirty in `git status`, invisible to `git diff`
# ---------------------------------------------------------------------------
#
# With check-in normalization on (core.autocrlf=input/true, or a `* text=auto`
# .gitattributes rule), a file whose only change is CRLF<->LF is listed by
# `git status --porcelain` as ` M` but produces an EMPTY `git diff HEAD` --
# git converts the working copy back to the committed blob on check-in, so
# there is no content change at all and `git commit` says "nothing to commit".
# get_diff() therefore returns "" and message generation has nothing to work
# with; describe_empty_diff() turns that dead end into an explanation.

MAX_EOL_PROBE_FILES = 50
MAX_EOL_PROBE_BYTES = 2_000_000  # don't slurp huge files just to compare EOLs
_EOL_MODIFIED_CODES = ("M", "MM", "AM", "RM", "MD")


def _eol_normalize(data):
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def is_eol_only_change(committed, working):
    """True when two byte strings differ *only* in their line terminators."""
    if committed == working:
        return False
    return _eol_normalize(committed) == _eol_normalize(working)


def normalizes_to_same_content(committed, working):
    """True when both byte strings are equal once line endings are normalized.

    Broader than :func:`is_eol_only_change`: it also covers byte-identical
    files, which git still reports as ` M` when the index's cached stat entry
    came from a differently-converted checkout (e.g. core.autocrlf=true stores
    LF but checks out CRLF, so an LF-ified working copy is dirty-but-diffless).
    """
    return _eol_normalize(committed) == _eol_normalize(working)


def describe_eol_style(data):
    """Name the line-ending style of *data*: CRLF / LF / CR / mixed."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    cr = data.count(b"\r") - crlf
    present = [name for name, count in (("CRLF", crlf), ("LF", lf), ("CR", cr)) if count]
    if not present:
        return "no line endings"
    if len(present) == 1:
        return present[0]
    return "mixed " + "+".join(present)


def _committed_blob(cwd, filepath):
    """Raw bytes of *filepath* as stored in HEAD, or None if unavailable."""
    rc, stdout, _ = run_git_bytes(["show", f"HEAD:{filepath}"], cwd=cwd)
    if rc != 0:
        return None
    return stdout


def find_eol_only_changes(cwd, entries=None, only_path=None):
    """Find tracked files git calls modified but that produce no diff.

    A file qualifies when its working copy normalizes to exactly the committed
    content *and* ``git diff HEAD -- <file>`` is empty -- the latter check keeps
    changes that merely happen to share their text (a mode change, say) out of
    the "it's only line endings" story.

    Returns a list of ``(filepath, committed_style, worktree_style)``.
    """
    if entries is None:
        ok, entries = read_status(cwd)
        if not ok:
            return []
    found = []
    for code, filepath in entries[:MAX_EOL_PROBE_FILES]:
        if code not in _EOL_MODIFIED_CODES:
            continue
        if only_path is not None and filepath != only_path:
            continue
        full = Path(cwd) / filepath
        try:
            if full.is_symlink() or not full.is_file():
                continue
            if full.stat().st_size > MAX_EOL_PROBE_BYTES:
                continue
            working = full.read_bytes()
        except OSError:
            continue
        committed = _committed_blob(cwd, filepath)
        if committed is None:
            continue
        if not normalizes_to_same_content(committed, working):
            continue
        rc, file_diff, _ = run_git(["diff", "HEAD", "--", filepath], cwd=cwd)
        if rc != 0 or file_diff.strip():
            continue
        found.append(
            (filepath, describe_eol_style(committed), describe_eol_style(working))
        )
    return found


def describe_empty_diff(cwd, only_path=None):
    """Explain an empty diff on a repo git still reports as dirty.

    Returns "" when there is nothing to explain -- a genuinely clean tree, or
    changes that do produce a diff.
    """
    ok, entries = read_status(cwd)
    if not ok or not entries:
        return ""
    eol = find_eol_only_changes(cwd, entries, only_path=only_path)
    if not eol:
        return ""
    rc, autocrlf, _ = run_git(["config", "--get", "core.autocrlf"], cwd=cwd)
    setting = autocrlf.strip() if rc == 0 and autocrlf.strip() else "unset"
    noun = "file differs" if len(eol) == 1 else "files differ"
    lines = [f"{len(eol)} {noun} only in line endings - nothing to commit."]
    for filepath, committed_style, working_style in eol:
        if committed_style == working_style:
            # Byte-identical to the blob: the checkout git expects is the
            # converted one (autocrlf=true stores LF, checks out CRLF).
            lines.append(
                f"  {filepath}: {working_style} on disk, matching the committed blob"
            )
        else:
            lines.append(
                f"  {filepath}: {committed_style} committed, {working_style} on disk"
            )
    lines.append(
        f"Git normalizes line endings on check-in (core.autocrlf={setting}), so the"
    )
    lines.append(
        'committed content is unchanged and `git commit` reports "nothing to commit".'
    )
    # `text eol=...` only controls the *checkout*; it leaves the blob normalized,
    # so it cannot make an EOL change committable. `-text` (no conversion) can.
    lines.append(
        "To store the on-disk endings instead, mark the path `-text` in .gitattributes"
    )
    lines.append(
        "and run `git add --renormalize .` (`text eol=...` only affects checkout)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI provider
# ---------------------------------------------------------------------------

def generate_message_ollama(diff, model, base_url):
    """Call Ollama chat API and return the generated commit message.

    Raises OllamaError on any failure instead of calling sys.exit().
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": diff},
        ],
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["message"]["content"]
        if not content:
            raise OllamaError(
                f"Model '{model}' returned empty content - it may have used "
                "thinking/tool-call mode instead of a plain text response."
            )
        return content.strip()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise OllamaError(
                f"Model '{model}' not found on Ollama.\n"
                f"Pull it with: ollama pull {model}"
            ) from exc
        raise OllamaError(f"Ollama returned HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"Could not reach Ollama at {base_url}\n"
            f"Is Ollama running? Start it with: ollama serve\n({exc})"
        ) from exc
    except (KeyError, json.JSONDecodeError) as exc:
        raise OllamaError(f"Unexpected response from Ollama: {exc}") from exc


class KiroCliError(Exception):
    """Raised when kiro-cli call fails."""
    pass


def generate_message_kiro(diff, model):
    """Call kiro-cli via WSL and return the generated commit message.

    Writes the prompt to a temp file and pipes it via stdin to kiro-cli
    to avoid shell escaping and argument-length issues.
    Raises KiroCliError on any failure.
    """
    import tempfile

    prompt = SYSTEM_PROMPT + "\n\n" + diff

    # Write prompt to a temp file on the Windows side
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    try:
        tmp.write(prompt)
        tmp.close()

        # Convert Windows path to WSL path
        win_path = tmp.name.replace("\\", "/")
        if len(win_path) >= 2 and win_path[1] == ":":
            wsl_path = f"/mnt/{win_path[0].lower()}{win_path[2:]}"
        else:
            wsl_path = win_path

        # Pipe file content into kiro-cli stdin (avoids bash arg-length limits).
        # shlex.quote both values -- they end up inside a bash -lc string, so an
        # unquoted model name from settings/env could inject shell commands.
        bash_cmd = (
            f"cat {shlex.quote(wsl_path)} | "
            f"kiro-cli chat --no-interactive --model {shlex.quote(model)} 2>/dev/null"
        )
        cmd = ["wsl", "--", "bash", "-lc", bash_cmd]

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                **kwargs,
            )
        except FileNotFoundError:
            raise KiroCliError(
                "Could not find 'wsl' command.\n"
                "Ensure WSL is installed and kiro-cli is available inside it."
            )
        except subprocess.TimeoutExpired:
            raise KiroCliError("kiro-cli timed out after 180 seconds.")
    finally:
        os.unlink(tmp.name)

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise KiroCliError(f"kiro-cli exited with code {result.returncode}:\n{err}")

    content = result.stdout.strip()
    if not content:
        raise KiroCliError("kiro-cli returned empty output.")
    # Strip ANSI escape codes from kiro-cli's colored output
    content = re.sub(r"\x1b\[[0-9;]*m", "", content)
    # Strip the leading "> " prefix kiro-cli adds to responses
    lines = content.splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r"^>\s?", "", line)
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def generate_message(diff, config):
    """Dispatch to the configured AI provider."""
    provider = config.get("provider", "ollama")
    if provider == "kiro":
        return generate_message_kiro(diff, config["model"])
    return generate_message_ollama(diff, config["model"], config["url"])


# ---------------------------------------------------------------------------
# Commit & push
# ---------------------------------------------------------------------------

# GitLab's documented push option to bypass secret push protection for one push.
SECRET_PUSH_SKIP_OPTION = "secret_push_protection.skip_all"


def is_secret_push_block(text):
    """True if a push failure is GitLab's secret-push-protection pre-receive
    block (the one that suggests `-o secret_push_protection.skip_all`)."""
    if not text:
        return False
    lowered = text.lower()
    return (SECRET_PUSH_SKIP_OPTION in lowered
            or "push blocked: secrets detected" in lowered)


def do_commit_and_push(cwd, message):
    """Stage all changes, commit, and attempt to push.

    Returns (committed: bool, pushed: bool, detail: str).
    """
    # Stage
    rc, _, stderr = run_git(["add", "-A"], cwd=cwd)
    if rc != 0:
        return False, False, f"git add failed: {stderr}"

    # Commit
    rc, stdout, stderr = run_git(["commit", "-m", message], cwd=cwd)
    if rc != 0:
        return False, False, f"git commit failed: {stderr}"

    detail = stdout.strip()

    # Push -- skip if no remote configured
    rc_remote, _, _ = run_git(["remote", "get-url", "origin"], cwd=cwd)
    if rc_remote != 0:
        return True, False, "LOCAL_ONLY"

    rc, push_out, push_err = run_git(["push"], cwd=cwd)
    if rc != 0:
        if "has no upstream branch" in push_err or "no upstream branch" in push_err:
            branch = get_current_branch(cwd)
            return True, False, f"NO_UPSTREAM:{branch}"
        detail += f"\nPush failed: {push_err.strip()}"
        return True, False, detail

    detail += "\nPushed successfully."
    return True, True, detail


# ---------------------------------------------------------------------------
# Repo discovery
# ---------------------------------------------------------------------------

def discover_repos(folder):
    """Return git repo paths to monitor.

    Always include *folder* itself if it is a repo root, plus any direct
    child directories that are repo roots. Supports the "umbrella" pattern
    where a folder is its own repo (for loose files) and also contains
    independent child repos.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        return []
    repos = []
    if is_git_repo(folder):
        repos.append(folder)
    for child in sorted(folder.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            if is_git_repo(child):
                repos.append(child)
    return repos


def default_config():
    """Return default config dict respecting env vars."""
    return {
        "provider": os.environ.get("AI_COMMIT_PROVIDER", "ollama"),
        "model": os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
        "url": os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
    }
