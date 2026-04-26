#!/usr/bin/env python3
"""Standalone GitHub Actions workflow viewer — runs as a separate OS window.

Launched by ai-commit-gui after a successful push. Reads connection params
from a temp JSON file passed as the first CLI argument, then polls the
GitHub API and displays live workflow status with per-step logs.
"""

import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import dearpygui.dearpygui as dpg

from gh_workflows import (
    Run, _api_get, cancel_run, detect_runs_for_commit,
    fetch_jobs, fetch_run_logs_zip, get_gh_token,
)

# ---------------------------------------------------------------------------
# Colors (match main GUI theme)
# ---------------------------------------------------------------------------

COL_BG = (30, 30, 35)
COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_YELLOW = (220, 180, 60)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)


def _status_icon(status, conclusion):
    if status == "completed":
        mapping = {
            "success": ("[OK]", COL_GREEN),
            "failure": ("[FAIL]", COL_RED),
            "cancelled": ("[--]", COL_DIM),
            "skipped": ("[SKIP]", COL_DIM),
            "timed_out": ("[TIMEOUT]", COL_RED),
        }
        return mapping.get(conclusion, ("[?]", COL_DIM))
    mapping = {
        "queued": ("[ ]", COL_DIM),
        "in_progress": (">>>", COL_YELLOW),
        "waiting": ("[..]", COL_DIM),
    }
    return mapping.get(status, ("...", COL_DIM))


def _elapsed(started_at, completed_at=None):
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end = (datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
               if completed_at
               else datetime.now(timezone.utc))
        secs = max(0, int((end - start).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------

class Viewer:
    def __init__(self, owner, repo, sha, token):
        self.owner = owner
        self.repo = repo
        self.sha = sha
        self.sha_short = sha[:7] if len(sha) > 7 else sha
        self.token = token

        self.ui_queue = queue.Queue()
        self.stop_event = threading.Event()

        self.runs = []
        self._run_tabs = {}
        self._step_widgets = {}
        self._step_log_text = {}
        self._last_zip_fetch = {}

    # -- entry point --------------------------------------------------------

    def run(self):
        dpg.create_context()
        self._create_theme()

        title = f"GitHub Actions — {self.owner}/{self.repo} @ {self.sha_short}"
        dpg.create_viewport(
            title=title, width=880, height=620,
            min_width=500, min_height=300,
        )

        with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                        no_move=True, no_close=True):
            self._status_tag = dpg.add_text(
                "Detecting workflow runs...", color=COL_YELLOW,
            )
            dpg.add_separator()
            with dpg.child_window(tag="content_area", autosize_x=True,
                                  height=-35, border=False):
                self._tab_bar = dpg.add_tab_bar()
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(label="Open on GitHub",
                               callback=self._cb_open_github)
                dpg.add_button(label="Close", callback=self._cb_close)

        dpg.set_primary_window("primary", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()

        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

        while dpg.is_dearpygui_running():
            self._process_queue()
            dpg.render_dearpygui_frame()

        self.stop_event.set()
        dpg.destroy_context()

    # -- theme --------------------------------------------------------------

    def _create_theme(self):
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (35, 35, 40))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 42, 55))
                dpg.add_theme_color(dpg.mvThemeCol_Header, (50, 55, 70))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (60, 65, 85))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (70, 75, 95))
                dpg.add_theme_color(dpg.mvThemeCol_Tab, (45, 48, 60))
                dpg.add_theme_color(dpg.mvThemeCol_TabHovered, (65, 70, 90))
                dpg.add_theme_color(dpg.mvThemeCol_TabActive, (75, 80, 105))
                dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (25, 25, 30))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (60, 60, 75))
                dpg.add_theme_color(dpg.mvThemeCol_Separator, (55, 55, 65))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (30, 30, 35))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (40, 42, 55))
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 4)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 6)
                dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 10)
        dpg.bind_theme(theme)

    # -- callbacks ----------------------------------------------------------

    def _cb_open_github(self, sender=None, app_data=None, user_data=None):
        if self.runs:
            webbrowser.open(self.runs[0].html_url)
        else:
            webbrowser.open(
                f"https://github.com/{self.owner}/{self.repo}/actions"
            )

    def _cb_close(self, sender=None, app_data=None, user_data=None):
        dpg.stop_dearpygui()

    def _cb_cancel_run(self, sender, app_data, user_data):
        run_id = user_data
        threading.Thread(
            target=lambda: cancel_run(
                self.owner, self.repo, run_id, self.token
            ),
            daemon=True,
        ).start()

    # -- background polling -------------------------------------------------

    def _poll_loop(self):
        runs = detect_runs_for_commit(
            self.owner, self.repo, self.sha, self.token,
            cancel_event=self.stop_event,
        )
        if self.stop_event.is_set():
            return
        if not runs:
            self.ui_queue.put(("no_runs",))
            return

        self.runs = runs
        self.ui_queue.put(("runs_found", runs))

        while not self.stop_event.is_set():
            all_complete = True

            for run in self.runs:
                if self.stop_event.is_set():
                    return

                try:
                    rd = _api_get(
                        f"/repos/{self.owner}/{self.repo}/actions/runs/{run.id}",
                        self.token,
                    )
                    ns, nc = rd.get("status", run.status), rd.get("conclusion")
                    if ns != run.status or nc != run.conclusion:
                        run.status, run.conclusion = ns, nc
                        self.ui_queue.put(("run_status", run.id, ns, nc))
                except Exception:
                    pass

                if run.status != "completed":
                    all_complete = False

                jobs = fetch_jobs(
                    self.owner, self.repo, run.id, self.token,
                )
                run.jobs = jobs
                self.ui_queue.put(("jobs_update", run.id, jobs))

                now = time.monotonic()
                if now - self._last_zip_fetch.get(run.id, 0) > 5:
                    self._fetch_logs(run)
                    self._last_zip_fetch[run.id] = now

            if all_complete:
                for run in self.runs:
                    self._fetch_logs(run)
                self.ui_queue.put(("all_complete",))
                return

            self.stop_event.wait(2.0)

    def _fetch_logs(self, run):
        logs = fetch_run_logs_zip(
            self.owner, self.repo, run.id, self.token,
        )
        if logs:
            self.ui_queue.put(("logs_available", run.id, logs))

    # -- queue processing (main thread) -------------------------------------

    def _process_queue(self):
        while True:
            try:
                msg = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            kind = msg[0]

            if kind == "no_runs":
                dpg.set_value(
                    self._status_tag,
                    "No workflow runs triggered by this commit.",
                )
                dpg.configure_item(self._status_tag, color=COL_DIM)

            elif kind == "runs_found":
                runs = msg[1]
                dpg.set_value(
                    self._status_tag,
                    f"Found {len(runs)} workflow run(s)",
                )
                dpg.configure_item(self._status_tag, color=COL_GREEN)
                for r in runs:
                    self._add_run_tab(r)

            elif kind == "run_status":
                _, run_id, status, conclusion = msg
                self._update_run_header(run_id, status, conclusion)

            elif kind == "jobs_update":
                _, run_id, jobs = msg
                self._update_steps(run_id, jobs)

            elif kind == "logs_available":
                _, run_id, logs = msg
                self._fill_step_logs(run_id, logs)

            elif kind == "all_complete":
                dpg.set_value(self._status_tag, "All runs complete.")
                dpg.configure_item(self._status_tag, color=COL_GREEN)

    # -- UI builders --------------------------------------------------------

    def _add_run_tab(self, run):
        if run.id in self._run_tabs:
            return

        tab_tag = dpg.generate_uuid()
        header_tag = dpg.generate_uuid()
        steps_group = dpg.generate_uuid()

        label = f"{run.workflow_name or run.name} #{run.run_number}"
        icon, color = _status_icon(run.status, run.conclusion)
        header_text = f"{icon} {run.status}"
        if run.head_branch:
            header_text += f" · branch: {run.head_branch}"
        el = _elapsed(run.created_at)
        if el:
            header_text += f" · {el}"

        with dpg.tab(label=label, tag=tab_tag, parent=self._tab_bar):
            dpg.add_text(header_text, tag=header_tag, color=color)
            dpg.add_separator()
            with dpg.child_window(
                tag=steps_group, autosize_x=True, height=-40,
                border=False,
            ):
                dpg.add_text("Loading jobs...", color=COL_DIM,
                             tag=f"placeholder_{run.id}")
            dpg.add_separator()
            with dpg.group(horizontal=True):
                dpg.add_button(
                    label="Open on GitHub",
                    callback=lambda s, a, u: webbrowser.open(u),
                    user_data=run.html_url,
                )
                dpg.add_button(
                    label="Cancel Run",
                    callback=self._cb_cancel_run,
                    user_data=run.id,
                )

        self._run_tabs[run.id] = {
            "tab_tag": tab_tag,
            "header_tag": header_tag,
            "steps_group": steps_group,
            "built": False,
        }

    def _update_run_header(self, run_id, status, conclusion):
        info = self._run_tabs.get(run_id)
        if not info:
            return
        ht = info["header_tag"]
        if not dpg.does_item_exist(ht):
            return

        icon, color = _status_icon(status, conclusion)
        text = f"{icon} {status}"
        if conclusion:
            text += f" — {conclusion}"
        for r in self.runs:
            if r.id == run_id:
                if r.head_branch:
                    text += f" · branch: {r.head_branch}"
                el = _elapsed(r.created_at)
                if el:
                    text += f" · {el}"
                tab_label = f"{r.workflow_name or r.name} #{r.run_number}"
                if conclusion:
                    tab_label += f" ({conclusion})"
                if dpg.does_item_exist(info["tab_tag"]):
                    dpg.configure_item(info["tab_tag"], label=tab_label)
                break

        dpg.set_value(ht, text)
        dpg.configure_item(ht, color=color)

    def _update_steps(self, run_id, jobs):
        info = self._run_tabs.get(run_id)
        if not info:
            return
        parent = info["steps_group"]
        if not dpg.does_item_exist(parent):
            return

        if not info["built"]:
            ph = f"placeholder_{run_id}"
            if dpg.does_item_exist(ph):
                dpg.delete_item(ph)
            info["built"] = True

        for job in jobs:
            for step in job.steps:
                key = f"{run_id}:{job.id}:{step.number}"
                if key not in self._step_widgets:
                    self._create_step(key, parent, job, step)
                else:
                    self._update_step(key, step)

    def _create_step(self, key, parent, job, step):
        icon, color = _status_icon(step.status, step.conclusion)
        el = _elapsed(step.started_at, step.completed_at)
        label = f"{icon} {step.name}"
        if el:
            label += f"  {el}"

        header_tag = dpg.generate_uuid()
        log_tag = dpg.generate_uuid()

        with dpg.collapsing_header(
            label=label, tag=header_tag, parent=parent,
            default_open=False,
        ):
            dpg.add_input_text(
                tag=log_tag,
                default_value="",
                multiline=True, readonly=True,
                width=-1, height=200,
                show=True,
            )

        self._step_widgets[key] = {
            "header_tag": header_tag,
            "log_tag": log_tag,
            "status": step.status,
            "conclusion": step.conclusion,
            "job_name": job.name,
            "step_number": step.number,
        }

    def _update_step(self, key, step):
        w = self._step_widgets[key]
        icon, color = _status_icon(step.status, step.conclusion)
        el = _elapsed(step.started_at, step.completed_at)
        label = f"{icon} {step.name}"
        if el:
            label += f"  {el}"
        if dpg.does_item_exist(w["header_tag"]):
            dpg.configure_item(w["header_tag"], label=label)
        w["status"] = step.status
        w["conclusion"] = step.conclusion

    def _fill_step_logs(self, run_id, logs):
        for key, w in self._step_widgets.items():
            if not key.startswith(f"{run_id}:"):
                continue
            if key in self._step_log_text:
                continue

            step_num = w["step_number"]
            job_name = w["job_name"]

            log_text = None
            for (job_dir, snum), text in logs.items():
                if snum == step_num:
                    if (job_name.lower() in job_dir.lower()
                            or job_dir.lower() in job_name.lower()):
                        log_text = text
                        break

            if log_text is None:
                for (job_dir, snum), text in logs.items():
                    if snum == step_num:
                        log_text = text
                        break

            if not log_text:
                continue

            lt = w["log_tag"]
            if not dpg.does_item_exist(lt):
                continue

            self._step_log_text[key] = log_text

            lines = log_text.splitlines()
            display = log_text
            if len(lines) > 2000:
                display = "\n".join(
                    ["... (truncated, showing last 2000 lines) ..."]
                    + lines[-2000:]
                )
                lines = lines[-2000:]

            dpg.set_value(lt, display)
            height = max(80, min(len(lines) * 15 + 20, 400))
            dpg.configure_item(lt, height=height)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: gh_workflow_viewer.py <data.json>")
        sys.exit(1)

    data_path = Path(sys.argv[1])
    try:
        data = json.loads(data_path.read_text())
    finally:
        try:
            data_path.unlink()
        except OSError:
            pass

    viewer = Viewer(
        owner=data["owner"],
        repo=data["repo"],
        sha=data["sha"],
        token=data["token"],
    )
    viewer.run()


if __name__ == "__main__":
    main()
