# Poll performance: detecting repo changes

Analysis + options for reducing the cost of ai-commit's per-cycle repo polling.
**Option 1 is implemented** (see [Status](#status)); Options 2–5 are documented
here for later reference.

## The problem

The GUI runs a polling loop (`bg_poll_repos` → `_poll_one_repo`) that, for every
watched repo every cycle, shells out to git many times. Each `git` invocation on
Windows spawns a process (~40 ms), and that spawn — not the git work — is the
dominant cost. With N repos this is N × (spawns) per cycle.

### What one cycle cost (before)

Per repo, `_poll_one_repo` ran ~6–9 git subprocesses:

| Call | Command | Purpose |
|------|---------|---------|
| discovery | `git rev-parse --show-toplevel` (`is_git_repo`) | confirm folder is a repo |
| status | `git status --porcelain` | uncommitted changes |
| last commit | `git log -1 --format=%ci%n%B` | header date + message |
| remote* | `git remote get-url origin` | remote URL (cached after 1st cycle) |
| visibility* | `gh repo view ...` | public/private (cached; this is `gh`, not git) |
| branch | `git rev-parse --abbrev-ref HEAD` | current branch |
| ahead/behind | `git rev-parse @{u}` + `git rev-list --left-right --count` | sync counts (2 calls) |
| classification | `git remote` + 2× `git for-each-ref` | local-only / stale (3 calls) |
| fetch† | `git fetch --prune --quiet` | only on new repo / forced refresh |

\* cached on `RepoState` after the first cycle, so usually skipped.
† network; already gated to `is_new or repo_force`, not every cycle.

Steady-state local git spawns: **~8 per repo per cycle.**

### Benchmark (ai-commit repo, Windows, 20 iterations)

| Approach | git spawns/repo | time/repo/cycle |
|----------|-----------------|-----------------|
| Before | 9 | **378 ms** |
| Folded (Option 1) | 2 | **81 ms** |

~4.7× faster, and it multiplies by repo count.

## Key insight: filesystem polling can't *replace* git here

The original question was "why not poll repo folders for on-disk changes instead
of running git constantly?" Filesystem mtime/scan only answers **one** axis —
"is the working tree dirty" (`git status`). Everything else the UI shows
(ahead/behind, branch classification, last commit, remote moves) is **not**
derivable from working-tree file mtimes; it needs git and/or the network. So FS
polling can at best *gate* the local git calls, not replace them — and doing it
naively means reimplementing `.gitignore`, excluding `.git` churn, and coping
with OneDrive rewriting mtimes. The cheap, safe win is to stop spawning
*redundant* git processes, which is what Option 1 does.

## The fold (why one call replaces four)

`git status --porcelain --branch` emits a `## ` header line that encodes branch,
ahead/behind, and the current branch's classification — then the normal porcelain
entries. Porcelain output is **guaranteed non-localized and stable**, so the
marker strings are safe to parse. Refnames can't contain `..`, so `...` is an
unambiguous branch/upstream separator. Every header shape (verified, and covered
by `tests/test_status_branch.py`):

| Header line | Meaning |
|-------------|---------|
| `## main...origin/main` | upstream live, synced |
| `## main...origin/main [ahead 1, behind 2]` | ahead/behind counts (also `[ahead N]` / `[behind M]` alone) |
| `## main...origin/main [gone]` | upstream deleted → **stale** |
| `## feature` (no `...`) | no upstream → **local only** |
| `## HEAD (no branch)` | detached HEAD |
| `## No commits yet on main` | unborn branch (fresh repo) |

So **status + branch + ahead/behind + classification = 4 calls → 1.** The poll
only ever used the *current* branch's classification, yet the old code built a
full all-branches dict (3 calls) and threw the rest away.

## Options (least → most effort; each builds on the prior)

### Option 1 — Status-fold only ✅ implemented
Replace `get_status` + `get_current_branch` + `get_sync_status` (local part) +
`get_branch_classification` with one `git status --porcelain --branch`, parsing
the header. v1 porcelain entry parser unchanged.
- Spawns: 9 → 3 (status + log + discovery), or → 2 with the network fetch folded in.
- Risk: low — pure header parsing (6 cases, unit-tested) + preserve the
  `ok=False` failure contract. No correctness change to what the numbers mean.

### Option 2 — Option 1 + `.git` discovery shortcut
Replace `is_git_repo` (`git rev-parse`) with `(path/".git").exists()` **for
already-known repos only**.
- Spawns: 9 → 2.
- Risk: won't catch mid-session corruption (the subsequent `git status` would,
  via `ok=False`); `.git` may be a *file* (worktrees/submodules) but
  `Path.exists()` handles that. Scope strictly to known repos — new folders
  still need git's real answer.

### Option 3 — Option 2 + porcelain v2 + SHA-cached last commit
Use `git status --porcelain=v2 --branch` (its header includes `# branch.oid
<sha>`) and cache the `git log` result keyed by HEAD SHA → skip `git log` when
HEAD hasn't moved.
- Spawns: down to **1** per idle cycle.
- Cost: rewrite the entry parser from v1 to v2 line format (more code/tests).

### Option 4 — Tier-1 mtime gate (the FS-poll idea, done right)
On top of any above: track newest working-tree mtime per repo; if unchanged
since last cycle, reuse the cached `RepoState` (the existing
`_cached_repo_result` path) and skip the local git calls entirely.
- Win: near-zero cost for idle repos.
- Risk: OneDrive rewrites mtimes / uses placeholder files (spurious events); does
  nothing for the network/sync axis.

### Option 5 — Full OS file-watcher
`ReadDirectoryChangesW` (Windows) / `watchdog` (cross-platform) → event-driven
instead of polling.
- Most work, most OneDrive-fragile. Not recommended as a first move.

## Status

**Implemented: Option 1.** Skipped the `.git` discovery shortcut (Option 2) to
keep the change minimal; revisit if idle CPU is still a concern.

### What changed
- `ai_commit_core.py`: added `parse_branch_header`, `read_status_branch`,
  `fetch_remote`. Left `read_status`, `get_status`, `get_current_branch`,
  `get_sync_status`, `get_branch_classification` intact — still used by the
  destructive branch-switch/create flows, the MORE-panel branch switcher, and
  `bg_refresh_then_generate`'s siblings.
- `ai-commit-gui.py`: new `_read_poll_status(rp, remote_url, do_fetch)` helper;
  the three poll sites (`_poll_one_repo`, `bg_refresh_single_repo`,
  `bg_refresh_then_generate`) now route through it. Dropped the now-unused
  `get_sync_status` import.
- Tests: `tests/test_status_branch.py` (new, 13 cases covering every header shape
  + the `ok=False` contract). `tests/test_poll_pause.py` updated to spy on
  `read_status_branch`/`fetch_remote` instead of the old `get_status`.

### Behavior preserved (verified)
- Parity check across 6 real repos: identical entries, branch, ahead/behind, and
  classification vs the old call sequence (0 mismatches).
- `ok=False` (git failed ≠ clean tree) contract kept in `read_status_branch`; the
  destructive flows still call the strict `read_status`, untouched.
- "Local only" badge still suppressed for repos with **no remote** (header shows
  LOCAL) but shown for a remote-repo branch with no upstream — `_read_poll_status`
  gates on `remote_url`, matching the old `get_branch_classification` behavior.

### Known minor differences
- On a git failure during the poll (e.g. losing an `index.lock` race), the folded
  path returns `branch=""` for that one cycle rather than fetching the branch via
  a separate call. Display-only and self-corrects next cycle.
- The no-remote gate keys on `origin` (`get_remote_url`) rather than "any remote"
  (`git remote`); a repo whose only remote isn't named `origin` would now show
  "local only". Negligible in practice.

## Re-run the tests
```bash
cd ai-commit
python tests/test_status_branch.py
python tests/test_poll_pause.py
python tests/test_status.py
```
