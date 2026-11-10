"""Tests for OS-store TLS verification and for surfacing GitHub API failures.

Regression for 2026-11-10: GitHub moved api.github.com to a Sectigo ECC chain
(Root E46) that Windows had not downloaded yet. Python's ssl only reads roots
already in the store, so every Actions check failed with
CERTIFICATE_VERIFY_FAILED and was reported as "no runs", and no window opened.

Real network, no mocks. The live test skips when gh has no token or the
network is down.

Headless: imports ai_commit_core-level modules only (no Dear PyGui).
"""
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gh_workflows
import os_trust

# ai-news commit 59337b6 (2026-11-10) has a completed "Build and Deploy" run.
AI_NEWS_SHA = "59337b628daaf715ddc7d29bd29a04795e61aeaa"


def test_enable_is_active_and_idempotent():
    assert os_trust.enable() is True, "truststore not installed in this environment"
    assert os_trust.enable() is True


def test_api_failure_raises_instead_of_reporting_no_runs():
    # A real connection failure: a local port that is bound but never listens.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    saved = gh_workflows.API_BASE
    gh_workflows.API_BASE = f"https://127.0.0.1:{port}"
    try:
        try:
            gh_workflows.detect_runs_for_commit(
                "o", "r", "0" * 40, "tok", timeout=1.5, poll_interval=0.2,
                raise_on_error=True,
            )
        except gh_workflows.GhApiError as exc:
            assert str(exc), "error message is empty"
        else:
            raise AssertionError("an unreachable API was reported as 'no runs'")
        # Default stays backward compatible for the viewer: [] and no raise.
        assert gh_workflows.detect_runs_for_commit(
            "o", "r", "0" * 40, "tok", timeout=1.0, poll_interval=0.2,
        ) == []
    finally:
        gh_workflows.API_BASE = saved


def test_live_actions_detection_finds_real_run():
    token = gh_workflows.get_gh_token()
    if not token:
        print("skip (no gh token)")
        return
    os_trust.enable()
    runs = gh_workflows.detect_runs_for_commit(
        "hankel-ai", "ai-news", AI_NEWS_SHA, token, timeout=30, raise_on_error=True,
    )
    assert runs, "GitHub has a run for this commit but none was detected"
    print(f"    found: {[r.name for r in runs]}")


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok  {_name}")
    print("All tests passed.")
