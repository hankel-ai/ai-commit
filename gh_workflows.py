"""GitHub Actions API client — run detection, job/step polling, log fetching.

Pure-Python module with no GUI imports.
"""

import io
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
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
    """Extract (owner, repo) from a GitHub HTTPS or SSH remote URL."""
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


def _api_get(path, token):
    """Authenticated GET to GitHub REST API. Returns parsed JSON."""
    url = f"{API_BASE}{path}" if path.startswith("/") else path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
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
        data = _api_get(
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs?per_page=100",
            token,
        )
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


def fetch_run_logs_zip(owner, repo, run_id, token):
    """Download the run-level log zip and return per-step log content.

    Returns dict mapping (job_dir_name, step_number) -> log_text.
    The zip contains files like "JobName/1_StepName.txt".
    """
    url = f"{API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/logs"
    try:
        raw = _api_get_raw(url, token)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return {}

    logs = {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                parts = name.split("/")
                if len(parts) < 2:
                    continue
                step_file = parts[-1]
                m = re.match(r"(\d+)_(.*?)\.txt$", step_file)
                if m:
                    step_num = int(m.group(1))
                    content = zf.read(name).decode("utf-8", errors="replace")
                    cleaned = []
                    for line in content.splitlines():
                        line = re.sub(
                            r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?", "", line
                        )
                        cleaned.append(line)
                    job_dir = "/".join(parts[:-1])
                    logs[(job_dir, step_num)] = "\n".join(cleaned)
    except (zipfile.BadZipFile, KeyError):
        pass

    return logs


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
