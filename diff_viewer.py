#!/usr/bin/env python3
"""Standalone diff viewer — runs as a separate OS window.

Launched by ai-commit-gui when the user clicks "View Diff" on a modified file.
Reads diff data from a temp JSON file passed as the first CLI argument.
"""

import json
import os
import sys
from pathlib import Path

import dearpygui.dearpygui as dpg

COL_BG = (30, 30, 35)
COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)

COL_DIFF_ADD = (80, 200, 100)
COL_DIFF_DEL = (220, 80, 80)
COL_DIFF_HDR = (100, 140, 230)
COL_DIFF_RANGE = (180, 140, 220)


def main():
    if len(sys.argv) < 2:
        print("Usage: diff_viewer.py <data.json>")
        sys.exit(1)

    data_path = Path(sys.argv[1])
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    finally:
        try:
            data_path.unlink()
        except OSError:
            pass

    filepath = data["filepath"]
    diff_text = data["diff"]

    dpg.create_context()

    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (50, 50, 60))
            dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (25, 25, 30))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 65, 85))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (75, 80, 105))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 2)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 6)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 10)
    dpg.bind_theme(theme)

    title = f"Diff - {filepath}"
    vp_kwargs = dict(
        title=title, width=900, height=700,
        min_width=400, min_height=300,
    )
    icon_path = str(Path(__file__).resolve().parent / "ai-commit-icon.ico")
    if os.path.isfile(icon_path):
        vp_kwargs["small_icon"] = icon_path
        vp_kwargs["large_icon"] = icon_path
    dpg.create_viewport(**vp_kwargs)

    with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                    no_move=True, no_close=True):
        dpg.add_text(filepath, color=COL_ACCENT)
        dpg.add_separator()

        with dpg.child_window(autosize_x=True, height=-35, border=False):
            for line in diff_text.splitlines():
                if line.startswith("+++") or line.startswith("---"):
                    dpg.add_text(line, color=COL_DIFF_HDR)
                elif line.startswith("@@"):
                    dpg.add_text(line, color=COL_DIFF_RANGE)
                elif line.startswith("+"):
                    dpg.add_text(line, color=COL_DIFF_ADD)
                elif line.startswith("-"):
                    dpg.add_text(line, color=COL_DIFF_DEL)
                elif line.startswith("diff "):
                    dpg.add_text(line, color=COL_DIFF_HDR)
                else:
                    dpg.add_text(line, color=COL_DIM)

        dpg.add_separator()
        dpg.add_button(label="Close", callback=lambda: dpg.stop_dearpygui())

    dpg.set_primary_window("primary", True)
    dpg.setup_dearpygui()
    dpg.show_viewport()

    while dpg.is_dearpygui_running():
        dpg.render_dearpygui_frame()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
