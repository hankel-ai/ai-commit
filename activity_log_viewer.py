#!/usr/bin/env python3
"""Standalone activity-log viewer — runs as a separate OS window.

Launched by ai-commit-gui ("Activity Log" button). Tails the JSON Lines file
written by ``activity_log`` so the user can watch, in real time, every git
command the app runs under the hood plus high-level app events (poll, generate,
commit, push, pull).

It reads the shared log file directly rather than sharing memory with the main
process, so it stays a fully independent window that survives across app
actions. An optional CLI argument overrides the log path (used by tests).
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import dearpygui.dearpygui as dpg

from activity_log import (
    CAT_AI, CAT_ERROR, CAT_EVENT, CAT_GIT, LOG_PATH, FileTailer,
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

_CAT_COLOR = {
    CAT_GIT: COL_ACCENT,
    CAT_EVENT: COL_GREEN,
    CAT_AI: COL_YELLOW,
    CAT_ERROR: COL_RED,
}
_CAT_LABEL = {
    CAT_GIT: "GIT  ",
    CAT_EVENT: "EVENT",
    CAT_AI: "AI   ",
    CAT_ERROR: "ERROR",
}

_MAX_ROWS = 2000  # cap rendered rows so a long-running session stays responsive


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (ValueError, OSError, TypeError):
        return "--:--:--"


class ActivityViewer:
    def __init__(self, path):
        self.path = path
        self.tailer = FileTailer(path)
        self._all = []          # every entry seen this session
        self._auto_scroll = True
        self._paused = False
        self._cat_enabled = {
            CAT_GIT: True, CAT_EVENT: True, CAT_AI: True, CAT_ERROR: True,
        }
        self._search = ""
        self._rows_tag = None
        self._status_tag = None

    # -- filtering ----------------------------------------------------------

    def _passes(self, entry):
        if not self._cat_enabled.get(entry.get("category"), True):
            return False
        if self._search:
            hay = " ".join(str(entry.get(k, "")) for k in ("message", "repo", "detail"))
            if self._search.lower() not in hay.lower():
                return False
        return True

    # -- rendering ----------------------------------------------------------

    def _add_row(self, entry):
        ts = _fmt_ts(entry.get("ts"))
        cat = entry.get("category", CAT_EVENT)
        color = _CAT_COLOR.get(cat, COL_WHITE)
        label = _CAT_LABEL.get(cat, cat.upper()[:5])
        repo = entry.get("repo") or ""
        rc = entry.get("rc")
        dur = entry.get("duration_ms")
        parts = [f"{ts}", f"[{label}]"]
        if repo:
            parts.append(f"{repo}:")
        line = " ".join(parts) + f" {entry.get('message', '')}"
        if dur is not None:
            line += f"  ({dur} ms)"
        failed = rc not in (None, 0)
        row_color = COL_RED if failed else color
        with dpg.group(parent=self._rows_tag):
            dpg.add_text(line, color=row_color, wrap=0)
            detail = entry.get("detail")
            if failed and detail:
                dpg.add_text(f"        rc={rc}: {detail}", color=COL_RED, wrap=0)

    def _rebuild(self):
        """Re-render all rows from scratch (after a filter change)."""
        if self._rows_tag is None:
            return
        dpg.delete_item(self._rows_tag, children_only=True)
        visible = [e for e in self._all if self._passes(e)][-_MAX_ROWS:]
        for entry in visible:
            self._add_row(entry)
        self._update_status()

    def _update_status(self):
        if self._status_tag is None:
            return
        shown = sum(1 for e in self._all if self._passes(e))
        state = "PAUSED" if self._paused else "live"
        dpg.set_value(
            self._status_tag,
            f"{shown} shown / {len(self._all)} total  -  {state}  -  {self.path}",
        )

    # -- callbacks ----------------------------------------------------------

    def _cb_cat(self, sender, value, cat):
        self._cat_enabled[cat] = value
        self._rebuild()

    def _cb_search(self, sender, value):
        self._search = value or ""
        self._rebuild()

    def _cb_auto(self, sender, value):
        self._auto_scroll = value

    def _cb_pause(self, sender, value):
        self._paused = value
        self._update_status()

    def _cb_clear(self):
        """Clear the on-screen view only — does not touch the log file."""
        self._all = []
        self._rebuild()

    # -- main loop ----------------------------------------------------------

    def _poll(self):
        if self._paused:
            return
        new = self.tailer.read_new()
        if not new:
            return
        appended_visible = False
        for entry in new:
            self._all.append(entry)
            if self._passes(entry):
                self._add_row(entry)
                appended_visible = True
        if len(self._all) > _MAX_ROWS * 2:
            self._all = self._all[-_MAX_ROWS:]
        self._update_status()
        if appended_visible and self._auto_scroll:
            dpg.set_y_scroll(self._rows_tag, dpg.get_y_scroll_max(self._rows_tag))

    def _create_theme(self):
        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (24, 24, 28))
        dpg.bind_theme(theme)

    def run(self):
        dpg.create_context()
        self._create_theme()

        vp_kwargs = dict(
            title="ai-commit - Activity Log", width=860, height=620,
            min_width=480, min_height=260,
        )
        icon_path = str(Path(__file__).resolve().parent / "ai-commit-icon.ico")
        if os.path.isfile(icon_path):
            vp_kwargs["small_icon"] = icon_path
            vp_kwargs["large_icon"] = icon_path
        dpg.create_viewport(**vp_kwargs)

        with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                        no_move=True, no_close=True):
            with dpg.group(horizontal=True):
                dpg.add_text("Filter:", color=COL_DIM)
                for cat in (CAT_GIT, CAT_EVENT, CAT_AI, CAT_ERROR):
                    dpg.add_checkbox(
                        label=_CAT_LABEL[cat].strip(), default_value=True,
                        callback=self._cb_cat, user_data=cat,
                    )
                dpg.add_spacer(width=12)
                dpg.add_checkbox(label="Auto-scroll", default_value=True,
                                 callback=self._cb_auto)
                dpg.add_checkbox(label="Pause", default_value=False,
                                 callback=self._cb_pause)
                dpg.add_button(label="Clear view", callback=self._cb_clear)
            dpg.add_input_text(hint="search messages / repos / errors...",
                               width=-1, callback=self._cb_search)
            dpg.add_separator()
            self._rows_tag = dpg.add_child_window(autosize_x=True, height=-28,
                                                  border=False)
            dpg.add_separator()
            self._status_tag = dpg.add_text("", color=COL_DIM)

        dpg.set_primary_window("primary", True)
        dpg.setup_dearpygui()
        dpg.show_viewport()

        # Seed with whatever is already in the file, then tail.
        self._poll()
        self._rebuild()

        while dpg.is_dearpygui_running():
            self._poll()
            dpg.render_dearpygui_frame()

        dpg.destroy_context()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else str(LOG_PATH)
    ActivityViewer(path).run()


if __name__ == "__main__":
    main()
