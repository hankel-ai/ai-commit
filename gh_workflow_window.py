"""Dear PyGui popup window for live GitHub Actions workflow monitoring."""

import time
import webbrowser
from datetime import datetime, timezone

import dearpygui.dearpygui as dpg


COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_YELLOW = (220, 180, 60)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)

STATUS_ICONS = {
    "queued": ("[ ]", COL_DIM),
    "in_progress": (">>>", COL_YELLOW),
    "completed": None,
}

CONCLUSION_ICONS = {
    "success": ("[OK]", COL_GREEN),
    "failure": ("[X]", COL_RED),
    "cancelled": ("[--]", COL_DIM),
    "skipped": ("[-]", COL_DIM),
    "timed_out": ("[X]", COL_RED),
    "action_required": ("[!]", COL_YELLOW),
}


def _status_display(status, conclusion):
    if status == "completed" and conclusion:
        icon, color = CONCLUSION_ICONS.get(conclusion, ("[?]", COL_DIM))
        return icon, color
    icon, color = STATUS_ICONS.get(status, ("...", COL_DIM))
    return icon, color


def _elapsed_str(started_at, completed_at=None):
    if not started_at:
        return ""
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if completed_at:
            end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        else:
            end = datetime.now(timezone.utc)
        secs = max(0, int((end - start).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except (ValueError, TypeError):
        return ""


class WorkflowWindow:
    """Manages the GitHub Actions popup window and its state."""

    def __init__(self, owner, repo, sha, on_cancel_run, on_close):
        self.owner = owner
        self.repo = repo
        self.sha = sha[:7] if len(sha) > 7 else sha
        self.full_sha = sha
        self._on_cancel_run = on_cancel_run
        self._on_close = on_close

        self.win_tag = dpg.generate_uuid()
        self.tab_bar_tag = dpg.generate_uuid()
        self.status_text_tag = dpg.generate_uuid()

        self._run_tabs = {}
        self._step_widgets = {}
        self._step_logs = {}
        self._step_log_text = {}
        self._run_status = {}

        self._build_window()

    def _build_window(self):
        with dpg.window(
            label=f"GitHub Actions — {self.owner}/{self.repo} @ {self.sha}",
            tag=self.win_tag,
            width=860, height=580,
            no_collapse=True,
            on_close=self._handle_close,
        ):
            dpg.add_tab_bar(tag=self.tab_bar_tag)

    def _handle_close(self, sender, app_data, user_data):
        self._on_close()
        if dpg.does_item_exist(self.win_tag):
            dpg.delete_item(self.win_tag)

    def destroy(self):
        if dpg.does_item_exist(self.win_tag):
            dpg.delete_item(self.win_tag)

    def add_run_tab(self, run):
        """Add a tab for a workflow run."""
        if run.id in self._run_tabs:
            return
        tab_tag = dpg.generate_uuid()
        self._run_tabs[run.id] = {
            "tab_tag": tab_tag,
            "status_tag": None,
            "jobs_group_tag": None,
            "run": run,
        }
        self._run_status[run.id] = (run.status, run.conclusion)

        tab_label = f"{run.workflow_name or run.name} #{run.run_number}"

        with dpg.tab(label=tab_label, tag=tab_tag, parent=self.tab_bar_tag):
            status_tag = dpg.generate_uuid()
            icon, color = _status_display(run.status, run.conclusion)
            elapsed = _elapsed_str(run.created_at)
            status_line = f"{icon} {run.status}"
            if elapsed:
                status_line += f" · {elapsed}"
            if run.head_branch:
                status_line += f" · branch: {run.head_branch}"
            dpg.add_text(status_line, tag=status_tag, color=color)
            dpg.add_separator()

            jobs_group_tag = dpg.generate_uuid()
            dpg.add_group(tag=jobs_group_tag)

            dpg.add_separator()
            with dpg.group(horizontal=True):
                open_btn = dpg.add_button(
                    label="Open on GitHub",
                    callback=lambda: webbrowser.open(run.html_url),
                )
                cancel_btn = dpg.add_button(
                    label="Cancel Run",
                    callback=self._cb_cancel_run,
                    user_data=run.id,
                )
                close_btn = dpg.add_button(
                    label="Close",
                    callback=lambda s, a, u: self._handle_close(s, a, u),
                )

        self._run_tabs[run.id]["status_tag"] = status_tag
        self._run_tabs[run.id]["jobs_group_tag"] = jobs_group_tag

    def _cb_cancel_run(self, sender, app_data, user_data):
        run_id = user_data
        self._on_cancel_run(run_id)

    def update_run_status(self, run_id, status, conclusion):
        """Update the status line for a run tab."""
        info = self._run_tabs.get(run_id)
        if not info or not info["status_tag"]:
            return
        if not dpg.does_item_exist(info["status_tag"]):
            return

        self._run_status[run_id] = (status, conclusion)
        run = info["run"]
        icon, color = _status_display(status, conclusion)
        elapsed = _elapsed_str(run.created_at)
        status_line = f"{icon} {status}"
        if conclusion:
            status_line += f" — {conclusion}"
        if elapsed:
            status_line += f" · {elapsed}"
        if run.head_branch:
            status_line += f" · branch: {run.head_branch}"

        dpg.set_value(info["status_tag"], status_line)
        dpg.configure_item(info["status_tag"], color=color)

        tab_label = f"{run.workflow_name or run.name} #{run.run_number}"
        if conclusion:
            tab_label += f" ({conclusion})"
        if dpg.does_item_exist(info["tab_tag"]):
            dpg.configure_item(info["tab_tag"], label=tab_label)

    def update_step(self, run_id, job_id, step_number, step_name,
                    status, conclusion, started_at, completed_at):
        """Update or create a step row in a run's jobs area."""
        info = self._run_tabs.get(run_id)
        if not info:
            return
        parent = info["jobs_group_tag"]
        if not dpg.does_item_exist(parent):
            return

        key = f"{run_id}:{job_id}:{step_number}"

        if key not in self._step_widgets:
            self._create_step_row(key, parent, job_id, step_number, step_name,
                                  status, conclusion, started_at, completed_at)
        else:
            self._update_step_row(key, status, conclusion, started_at, completed_at)

    def _create_step_row(self, key, parent, job_id, step_number, step_name,
                         status, conclusion, started_at, completed_at):
        icon, color = _status_display(status, conclusion)
        elapsed = _elapsed_str(started_at, completed_at)

        row_tag = dpg.generate_uuid()
        icon_tag = dpg.generate_uuid()
        elapsed_tag = dpg.generate_uuid()
        log_tag = dpg.generate_uuid()

        with dpg.group(parent=parent, tag=row_tag):
            with dpg.group(horizontal=True):
                dpg.add_text(icon, tag=icon_tag, color=color)
                dpg.add_text(f"{step_name}")
                dpg.add_text(elapsed if elapsed else "", tag=elapsed_tag, color=COL_DIM)

            dpg.add_input_text(
                tag=log_tag,
                default_value="",
                multiline=True, readonly=True,
                width=-1, height=0,
                show=False,
            )

        self._step_widgets[key] = {
            "row_tag": row_tag,
            "icon_tag": icon_tag,
            "elapsed_tag": elapsed_tag,
            "log_tag": log_tag,
            "status": status,
            "step_name": step_name,
        }
        self._step_log_text[key] = ""

    def _update_step_row(self, key, status, conclusion, started_at, completed_at):
        w = self._step_widgets[key]
        icon, color = _status_display(status, conclusion)
        elapsed = _elapsed_str(started_at, completed_at)

        if dpg.does_item_exist(w["icon_tag"]):
            dpg.set_value(w["icon_tag"], icon)
            dpg.configure_item(w["icon_tag"], color=color)
        if dpg.does_item_exist(w["elapsed_tag"]):
            dpg.set_value(w["elapsed_tag"], elapsed if elapsed else "")

        prev_status = w["status"]
        w["status"] = status

        if status == "in_progress" and prev_status != "in_progress":
            self._show_log_area(key)

    def _show_log_area(self, key):
        w = self._step_widgets[key]
        if dpg.does_item_exist(w["log_tag"]):
            dpg.configure_item(w["log_tag"], show=True, height=200)

    def append_step_log(self, run_id, job_id, step_number, text, is_final):
        """Append or replace log text for a step."""
        key = f"{run_id}:{job_id}:{step_number}"
        w = self._step_widgets.get(key)
        if not w:
            return
        if not dpg.does_item_exist(w["log_tag"]):
            return

        if is_final:
            self._step_log_text[key] = text
        else:
            self._step_log_text[key] = text

        display_text = self._step_log_text[key]
        lines = display_text.splitlines()
        if len(lines) > 500:
            display_text = "\n".join(
                ["... (earlier lines truncated) ..."] + lines[-500:]
            )

        dpg.set_value(w["log_tag"], display_text)

        line_count = min(len(lines), 500)
        height = max(80, min(line_count * 16 + 20, 400))
        dpg.configure_item(w["log_tag"], show=True, height=height)

    def mark_run_complete(self, run_id, conclusion):
        self.update_run_status(run_id, "completed", conclusion)

        for key, w in self._step_widgets.items():
            if key.startswith(f"{run_id}:"):
                if self._step_log_text.get(key):
                    self._show_log_area(key)

    def exists(self):
        return dpg.does_item_exist(self.win_tag)
