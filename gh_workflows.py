"""GitHub Actions workflow run detection, polling, and log streaming.

Pure-Python module — no Dear PyGui imports. Posts updates to a queue.Queue
for the GUI thread to consume.
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Step:
    number: int
    name: str
    status: str  # queued, in_progress, completed
    conclusion: Optional[str]  # success, failure, cancelled, skipped
    started_at: str = ""
    completed_at: str = ""


@dataclass
class Job:
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    html_url: str
    steps: list = field(default_factory=list)


@dataclass
class Run:
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    html_url: str
    jobs_url: str
    head_branch: str
    run_number: int
    created_at: str
    workflow_name: str = ""
    jobs: list = field(default_factory=list)


API_BASE = "https://api.github.com"


def get_gh_token():
    """Get GitHub auth token from the gh CLI."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
            **kwargs,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return ""


def parse_owner_repo(remote_url):
    """Extract (owner, repo) from a GitHub HTTPS or SSH remote URL.

    Expects the normalized HTTPS URL from ai_commit_core.get_remote_url().
    """
    if not remote_url:
        return "", ""
    url = remote_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        path = url.split(":", 1)[1]
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
        return "", ""
    if "://" in url:
        url = url.split("://", 1)[1]
    if "@" in url:
        url = url.split("@", 1)[1]
    parts = url.split("/")
    if len(parts) >= 3:
        return parts[1], parts[2]
    return "", ""


def _api_get(path, token, accept="application/vnd.github+json"):
    """Make an authenticated GET to the GitHub REST API. Returns parsed JSON."""
    url = f"{API_BASE}{path}" if path.startswith("/") else path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _api_get_raw(url, token):
    """GET a URL with auth, follow redirects, return bytes."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def detect_runs_for_commit(owner, repo, sha, token, *,
                           timeout=45, poll_interval=2.0, cancel_event=None):
    """Poll GitHub API until workflow runs appear for the given commit SHA.

    Returns a list of Run dataclasses. Empty list if none found within timeout.
    """
    deadline = time.monotonic() + timeout
    seen_ids = set()
    runs = []
    settle_polls = 0

    while time.monotonic() < deadline:
        if cancel_event and cancel_event.is_set():
            return runs

        try:
            data = _api_get(
                f"/repos/{owner}/{repo}/actions/runs?head_sha={sha}&per_page=20",
                token,
            )
        except (urllib.error.URLError, OSError):
            time.sleep(poll_interval)
            continue

        for wr in data.get("workflow_runs", []):
            if wr["id"] not in seen_ids:
                seen_ids.add(wr["id"])
                runs.append(Run(
                    id=wr["id"],
                    name=wr.get("name", wr.get("display_title", "")),
                    status=wr["status"],
                    conclusion=wr.get("conclusion"),
                    html_url=wr["html_url"],
                    jobs_url=wr["jobs_url"],
                    head_branch=wr.get("head_branch", ""),
                    run_number=wr.get("run_number", 0),
                    created_at=wr.get("created_at", ""),
                    workflow_name=wr.get("name", ""),
                ))

        if runs:
            settle_polls += 1
            if settle_polls >= 3:
                return runs

        time.sleep(poll_interval)

    return runs


def fetch_jobs(owner, repo, run_id, token):
    """Fetch jobs and steps for a given run. Returns list of Job."""
    try:
        data = _api_get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100", token)
    except (urllib.error.URLError, OSError):
        return []

    jobs = []
    for j in data.get("jobs", []):
        steps = []
        for s in j.get("steps", []):
            steps.append(Step(
                number=s["number"],
                name=s["name"],
                status=s["status"],
                conclusion=s.get("conclusion"),
                started_at=s.get("started_at", ""),
                completed_at=s.get("completed_at", ""),
            ))
        jobs.append(Job(
            id=j["id"],
            name=j["name"],
            status=j["status"],
            conclusion=j.get("conclusion"),
            html_url=j.get("html_url", ""),
            steps=steps,
        ))
    return jobs


def fetch_job_log(owner, repo, job_id, token):
    """Fetch the raw log text for a job.

    Works for in-progress and completed jobs. The API returns a 302 redirect
    to a signed URL that serves the log as plain text.
    Returns the log text as a string, or empty string on failure.
    """
    try:
        raw = _api_get_raw(
            f"{API_BASE}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            token,
        )
        return raw.decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return ""


def parse_job_log_by_step(log_text):
    """Parse a job's raw log text into per-step chunks.

    GitHub job logs use timestamp-prefixed lines. Step boundaries are marked
    by lines containing '##[group]' markers. Returns dict mapping step name
    to its log text.
    """
    if not log_text:
        return {}

    steps = {}
    current_step = None
    current_lines = []

    for line in log_text.splitlines():
        stripped = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", line)
        group_match = re.match(r"##\[group\](.*)", stripped)
        if group_match:
            if current_step is not None:
                steps[current_step] = "\n".join(current_lines)
            current_step = group_match.group(1).strip()
            current_lines = []
        elif current_step is not None:
            display_line = re.sub(r"##\[endgroup\]", "", stripped)
            current_lines.append(display_line)

    if current_step is not None:
        steps[current_step] = "\n".join(current_lines)

    return steps


def cancel_run(owner, repo, run_id, token):
    """Cancel a workflow run. Returns (success, detail)."""
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/cancel"
    req = urllib.request.Request(url, data=b"", method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Background workers — post messages to ui_queue
# ---------------------------------------------------------------------------

def _step_key(job_id, step_number):
    return f"{job_id}:{step_number}"


class RunWatcher:
    """Watches a single workflow run: polls jobs/steps, streams logs.

    All UI updates are posted to ui_queue as tuples. The GUI main thread
    processes them.
    """

    def __init__(self, owner, repo, run, token, ui_queue, cancel_event):
        self.owner = owner
        self.repo = repo
        self.run = run
        self.token = token
        self.ui_queue = ui_queue
        self.cancel_event = cancel_event
        self._prev_steps = {}
        self._fetched_logs = set()
        self._log_cache = {}

    def poll_loop(self):
        """Main polling loop. Call from a background thread."""
        while not self.cancel_event.is_set():
            try:
                run_data = _api_get(
                    f"/repos/{self.owner}/{self.repo}/actions/runs/{self.run.id}",
                    self.token,
                )
                new_status = run_data.get("status", self.run.status)
                new_conclusion = run_data.get("conclusion")
                if new_status != self.run.status or new_conclusion != self.run.conclusion:
                    self.run.status = new_status
                    self.run.conclusion = new_conclusion
                    self.ui_queue.put((
                        "workflow_run_update",
                        self.run.id, new_status, new_conclusion,
                    ))
            except (urllib.error.URLError, OSError):
                pass

            jobs = fetch_jobs(self.owner, self.repo, self.run.id, self.token)
            self.run.jobs = jobs

            for job in jobs:
                for step in job.steps:
                    key = _step_key(job.id, step.number)
                    prev = self._prev_steps.get(key)
                    if prev is None or prev.status != step.status or prev.conclusion != step.conclusion:
                        self._prev_steps[key] = step
                        self.ui_queue.put((
                            "workflow_step_update",
                            self.run.id, job.id, step.number,
                            step.name, step.status, step.conclusion,
                            step.started_at, step.completed_at,
                        ))

                    if step.status == "completed" and key not in self._fetched_logs:
                        self._fetch_step_log(job, step)

                if job.status == "in_progress":
                    self._stream_in_progress_logs(job)

            if self.run.status == "completed":
                for job in jobs:
                    for step in job.steps:
                        key = _step_key(job.id, step.number)
                        if key not in self._fetched_logs:
                            self._fetch_step_log(job, step)
                self.ui_queue.put((
                    "workflow_run_complete",
                    self.run.id, self.run.conclusion,
                ))
                return

            self.cancel_event.wait(2.0)

    def _fetch_step_log(self, job, step):
        """Fetch and post the full log for a completed step."""
        key = _step_key(job.id, step.number)
        if key in self._fetched_logs:
            return
        self._fetched_logs.add(key)

        log_text = self._get_job_log_cached(job.id)
        step_logs = parse_job_log_by_step(log_text)
        step_text = self._find_step_text(step_logs, step)
        if step_text:
            self.ui_queue.put((
                "workflow_step_log",
                self.run.id, job.id, step.number, step_text, True,
            ))

    def _stream_in_progress_logs(self, job):
        """Try to fetch partial logs for an in-progress job and post new lines."""
        log_text = fetch_job_log(self.owner, self.repo, job.id, self.token)
        if not log_text:
            return

        self._log_cache[job.id] = log_text
        step_logs = parse_job_log_by_step(log_text)

        for step in job.steps:
            if step.status != "in_progress":
                continue
            key = _step_key(job.id, step.number)
            step_text = self._find_step_text(step_logs, step)
            if step_text:
                self.ui_queue.put((
                    "workflow_step_log",
                    self.run.id, job.id, step.number, step_text, False,
                ))

    def _get_job_log_cached(self, job_id):
        """Fetch job log, using cache if available for completed jobs."""
        if job_id in self._log_cache:
            return self._log_cache[job_id]
        log_text = fetch_job_log(self.owner, self.repo, job_id, self.token)
        if log_text:
            self._log_cache[job_id] = log_text
        return log_text

    def _find_step_text(self, step_logs, step):
        """Find a step's log text by matching step name against parsed groups."""
        if step.name in step_logs:
            return step_logs[step.name]
        name_lower = step.name.lower()
        for group_name, text in step_logs.items():
            if name_lower in group_name.lower() or group_name.lower() in name_lower:
                return text
        return ""


def bg_watch_workflows(repo_name, repo_path, remote_url, ui_queue,
                       executor, cancel_event):
    """Top-level background worker: detect runs, then spawn per-run watchers.

    Called via executor.submit() after a successful push.
    """
    token = get_gh_token()
    if not token:
        ui_queue.put(("actions_unavailable", repo_name, "gh CLI not authenticated"))
        return

    owner, repo = parse_owner_repo(remote_url)
    if not owner or not repo:
        ui_queue.put(("actions_unavailable", repo_name,
                      f"Could not parse owner/repo from {remote_url}"))
        return

    from ai_commit_core import get_head_sha
    sha = get_head_sha(repo_path)
    if not sha:
        ui_queue.put(("actions_unavailable", repo_name, "Could not determine HEAD SHA"))
        return

    runs = detect_runs_for_commit(
        owner, repo, sha, token,
        cancel_event=cancel_event,
    )

    if not runs:
        return

    ui_queue.put(("workflow_runs_found", repo_name, owner, repo, sha, runs))

    for run in runs:
        watcher = RunWatcher(owner, repo, run, token, ui_queue, cancel_event)
        executor.submit(watcher.poll_loop)
