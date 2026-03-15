#!/usr/bin/env python3
"""AI Commit Monitor GUI — Dear PyGui desktop app for monitoring git repos."""

import argparse
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

import dearpygui.dearpygui as dpg

from ai_commit_core import (
    STATUS_LABELS,
    OllamaError,
    default_config,
    discover_repos,
    do_commit_and_push,
    generate_message,
    get_diff,
    get_status,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class GenStatus(Enum):
    IDLE = auto()
    GENERATING = auto()
    DONE = auto()
    ERROR = auto()


@dataclass
class RepoState:
    path: Path
    name: str
    entries: list  # list of (status_code, filepath)
    diff: str = ""
    commit_message: str = ""
    gen_status: GenStatus = GenStatus.IDLE
    error_message: str = ""
    # dpg widget tags
    header_tag: int = 0
    files_group_tag: int = 0
    input_tag: int = 0
    status_tag: int = 0
    gen_btn_tag: int = 0
    accept_btn_tag: int = 0
    regen_btn_tag: int = 0
    count_tag: int = 0


@dataclass
class AppState:
    watched_folder: Path = field(default_factory=lambda: Path("."))
    repos: dict = field(default_factory=dict)  # name -> RepoState
    poll_interval: int = 30
    auto_generate: bool = False
    model: str = "qwen3-coder:480b-cloud"
    ollama_url: str = "http://localhost:11434"
    last_poll: float = 0.0
    dragging: bool = False
    drag_offset_x: int = 0
    drag_offset_y: int = 0


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

app = AppState()
ui_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=4)

# Color palette
COL_BG = (30, 30, 35)
COL_HEADER_BG = (40, 40, 50)
COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_YELLOW = (220, 180, 60)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

def create_theme():
    with dpg.theme() as global_theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, COL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (50, 50, 60))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (70, 70, 85))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 65, 85))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (75, 80, 105))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Header, (45, 48, 62))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (55, 58, 75))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (65, 68, 85))
            dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (25, 25, 30))
            dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, COL_ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Separator, (55, 55, 65))
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 3)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 8, 6)
            dpg.add_theme_style(dpg.mvStyleVar_ScrollbarSize, 10)
    return global_theme


def create_button_theme(color):
    with dpg.theme() as t:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, color)
            r, g, b = color
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (min(r + 25, 255), min(g + 25, 255), min(b + 25, 255)))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (min(r + 40, 255), min(g + 40, 255), min(b + 40, 255)))
    return t


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def bg_poll_repos():
    """Discover repos and get status for each. Posts results to ui_queue."""
    folder = app.watched_folder
    repo_paths = discover_repos(folder)
    results = {}
    for rp in repo_paths:
        entries = get_status(rp)
        results[rp.name] = {"path": rp, "entries": entries}
    ui_queue.put(("poll_result", results))


def bg_generate_message(repo_name):
    """Generate commit message for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        diff = get_diff(rs.path)
        if not diff.strip():
            ui_queue.put(("gen_result", repo_name, "", "No diff content available."))
            return
        config = {"model": app.model, "url": app.ollama_url}
        msg = generate_message(diff, config)
        ui_queue.put(("gen_result", repo_name, msg, ""))
    except OllamaError as exc:
        ui_queue.put(("gen_result", repo_name, "", str(exc)))
    except Exception as exc:
        ui_queue.put(("gen_result", repo_name, "", f"Unexpected error: {exc}"))


def bg_commit_and_push(repo_name, message):
    """Commit and push for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        ok, detail = do_commit_and_push(rs.path, message)
        ui_queue.put(("commit_result", repo_name, ok, detail))
    except Exception as exc:
        ui_queue.put(("commit_result", repo_name, False, str(exc)))


# ---------------------------------------------------------------------------
# Drag-to-move
# ---------------------------------------------------------------------------

def mouse_down_handler(sender, app_data):
    mouse_x, mouse_y = dpg.get_mouse_pos(local=False)
    # Header region: top 28px of viewport
    vp_x = dpg.get_viewport_pos()[0]
    vp_y = dpg.get_viewport_pos()[1]
    if mouse_y < 28:
        app.dragging = True
        app.drag_offset_x = int(mouse_x) + vp_x
        app.drag_offset_y = int(mouse_y) + vp_y


def mouse_drag_handler(sender, app_data):
    if not app.dragging:
        return
    # app_data = [button, dx, dy] for drag
    mouse_x, mouse_y = dpg.get_mouse_pos(local=False)
    new_x = int(mouse_x) + app.drag_offset_x - int(mouse_x)
    new_y = int(mouse_y) + app.drag_offset_y - int(mouse_y)
    # Use global mouse position
    import ctypes
    try:
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        dpg.set_viewport_pos([pt.x - (app.drag_offset_x - dpg.get_viewport_pos()[0]),
                              pt.y - (app.drag_offset_y - dpg.get_viewport_pos()[1])])
    except Exception:
        pass


def mouse_release_handler(sender, app_data):
    app.dragging = False


# ---------------------------------------------------------------------------
# UI callbacks
# ---------------------------------------------------------------------------

def cb_browse(sender, app_data):
    dpg.show_item("folder_dialog")


def cb_folder_selected(sender, app_data):
    selections = app_data.get("selections", {})
    if selections:
        chosen = list(selections.values())[0]
    else:
        chosen = app_data.get("file_path_name", "")
    if chosen:
        folder = Path(chosen)
        if folder.is_dir():
            app.watched_folder = folder
            dpg.set_value("folder_label", str(folder))
            trigger_poll()


def cb_refresh(sender, app_data):
    trigger_poll()


def cb_poll_changed(sender, app_data):
    try:
        val = int(dpg.get_value(sender))
        if val < 5:
            val = 5
        app.poll_interval = val
    except (ValueError, TypeError):
        pass


def cb_auto_generate(sender, app_data):
    app.auto_generate = dpg.get_value(sender)


def cb_generate(sender, app_data, user_data):
    repo_name = user_data
    rs = app.repos.get(repo_name)
    if not rs or not rs.entries:
        return
    rs.gen_status = GenStatus.GENERATING
    rs.error_message = ""
    update_repo_status(rs)
    executor.submit(bg_generate_message, repo_name)


def cb_accept(sender, app_data, user_data):
    repo_name = user_data
    rs = app.repos.get(repo_name)
    if not rs:
        return
    message = dpg.get_value(rs.input_tag).strip()
    if not message:
        dpg.set_value(rs.status_tag, "No commit message.")
        dpg.configure_item(rs.status_tag, color=COL_RED)
        return
    rs.gen_status = GenStatus.GENERATING
    dpg.set_value(rs.status_tag, "Committing & pushing...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_commit_and_push, repo_name, message)


def cb_regen(sender, app_data, user_data):
    repo_name = user_data
    rs = app.repos.get(repo_name)
    if not rs or not rs.entries:
        return
    rs.gen_status = GenStatus.GENERATING
    rs.error_message = ""
    rs.commit_message = ""
    dpg.set_value(rs.input_tag, "")
    update_repo_status(rs)
    executor.submit(bg_generate_message, repo_name)


def cb_close(sender, app_data):
    """Hide window to tray instead of quitting."""
    try:
        import pystray
        # Move off-screen to hide
        dpg.set_viewport_pos([-10000, -10000])
    except ImportError:
        dpg.stop_dearpygui()


def cb_quit_real():
    dpg.stop_dearpygui()


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

tray_icon = None


def setup_tray():
    global tray_icon
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return  # No tray if pystray/Pillow not installed

    # Create a simple icon: blue circle on dark background
    img = Image.new("RGBA", (64, 64), (30, 30, 35, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([12, 12, 52, 52], fill=(100, 140, 230, 255))
    draw.text((22, 20), "AC", fill=(255, 255, 255, 255))

    def on_show(icon, item):
        ui_queue.put(("tray_show", None))

    def on_quit(icon, item):
        ui_queue.put(("tray_quit", None))
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Show", on_show, default=True),
        pystray.MenuItem("Quit", on_quit),
    )
    tray_icon = pystray.Icon("ai_commit_monitor", img, "AI Commit Monitor", menu)
    t = threading.Thread(target=tray_icon.run, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# UI builders
# ---------------------------------------------------------------------------

def trigger_poll():
    app.last_poll = time.time()
    executor.submit(bg_poll_repos)


def update_repo_status(rs):
    """Update the status text for a repo based on its gen_status."""
    if rs.gen_status == GenStatus.GENERATING:
        dpg.set_value(rs.status_tag, f"Generating with {app.model}...")
        dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    elif rs.gen_status == GenStatus.ERROR:
        dpg.set_value(rs.status_tag, f"Error: {rs.error_message}")
        dpg.configure_item(rs.status_tag, color=COL_RED)
    elif rs.gen_status == GenStatus.DONE:
        dpg.set_value(rs.status_tag, "Message ready.")
        dpg.configure_item(rs.status_tag, color=COL_GREEN)
    else:
        if rs.entries:
            dpg.set_value(rs.status_tag, "")
        else:
            dpg.set_value(rs.status_tag, "Clean")
            dpg.configure_item(rs.status_tag, color=COL_DIM)


def build_repo_section(rs, parent):
    """Build the UI section for a single repo inside *parent*."""
    change_count = len(rs.entries)
    label = f"{rs.name}/ ({change_count} change{'s' if change_count != 1 else ''})" if change_count else f"{rs.name}/ (clean)"

    rs.header_tag = dpg.add_collapsing_header(
        label=label,
        parent=parent,
        default_open=change_count > 0,
    )

    rs.files_group_tag = dpg.add_group(parent=rs.header_tag)
    for code, filepath in rs.entries:
        lbl = STATUS_LABELS.get(code, code)
        color = COL_GREEN if code in ("A", "AM", "??") else COL_YELLOW if code in ("M", "MM") else COL_RED if code == "D" else COL_DIM
        with dpg.group(horizontal=True, parent=rs.files_group_tag):
            dpg.add_text(f"  {lbl:>10}", color=color)
            dpg.add_text(f"  {filepath}")

    if not rs.entries:
        dpg.add_text("  No changes", color=COL_DIM, parent=rs.files_group_tag)

    # Commit message input
    if rs.entries:
        dpg.add_spacer(height=2, parent=rs.header_tag)
        rs.input_tag = dpg.add_input_text(
            default_value=rs.commit_message,
            hint="Commit message...",
            multiline=True,
            height=50,
            width=-1,
            parent=rs.header_tag,
        )

        # Status line
        rs.status_tag = dpg.add_text("", parent=rs.header_tag)
        update_repo_status(rs)

        # Buttons row
        with dpg.group(horizontal=True, parent=rs.header_tag):
            rs.gen_btn_tag = dpg.add_button(label="Generate", callback=cb_generate, user_data=rs.name)
            rs.accept_btn_tag = dpg.add_button(label="Accept & Push", callback=cb_accept, user_data=rs.name)
            rs.regen_btn_tag = dpg.add_button(label="Regen", callback=cb_regen, user_data=rs.name)

            dpg.bind_item_theme(rs.accept_btn_tag, green_btn_theme)

        dpg.add_spacer(height=4, parent=rs.header_tag)
    else:
        rs.status_tag = dpg.add_text("Clean", color=COL_DIM, parent=rs.header_tag)
        rs.input_tag = 0


def rebuild_repos_ui(results):
    """Rebuild repo sections from poll results, preserving user-edited messages."""
    # Preserve existing commit messages the user may have typed
    preserved_messages = {}
    for name, rs in app.repos.items():
        if rs.input_tag and dpg.does_item_exist(rs.input_tag):
            preserved_messages[name] = dpg.get_value(rs.input_tag)
        elif rs.commit_message:
            preserved_messages[name] = rs.commit_message

    # Preserve gen_status for repos that are still generating
    preserved_status = {}
    for name, rs in app.repos.items():
        if rs.gen_status == GenStatus.GENERATING:
            preserved_status[name] = rs.gen_status

    # Clear existing repo widgets
    if dpg.does_item_exist("repos_container"):
        dpg.delete_item("repos_container", children_only=True)

    # Update app.repos
    new_repos = {}
    for name, info in sorted(results.items()):
        old_rs = app.repos.get(name)
        rs = RepoState(
            path=info["path"],
            name=name,
            entries=info["entries"],
            commit_message=preserved_messages.get(name, ""),
            gen_status=preserved_status.get(name, GenStatus.DONE if preserved_messages.get(name) else GenStatus.IDLE),
            error_message=old_rs.error_message if old_rs else "",
        )
        new_repos[name] = rs
        build_repo_section(rs, "repos_container")

    app.repos = new_repos

    # Auto-generate for repos with changes and no message yet
    if app.auto_generate:
        for name, rs in app.repos.items():
            if rs.entries and not rs.commit_message and rs.gen_status == GenStatus.IDLE:
                rs.gen_status = GenStatus.GENERATING
                update_repo_status(rs)
                executor.submit(bg_generate_message, name)


# ---------------------------------------------------------------------------
# Queue processing
# ---------------------------------------------------------------------------

def process_queue():
    """Drain the UI queue and handle results. Called every frame."""
    while not ui_queue.empty():
        try:
            msg = ui_queue.get_nowait()
        except queue.Empty:
            break

        kind = msg[0]

        if kind == "poll_result":
            results = msg[1]
            rebuild_repos_ui(results)

        elif kind == "gen_result":
            _, repo_name, message, error = msg
            rs = app.repos.get(repo_name)
            if not rs:
                continue
            if error:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = error
                rs.commit_message = ""
            else:
                rs.gen_status = GenStatus.DONE
                rs.commit_message = message
                rs.error_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, message)
            update_repo_status(rs)

        elif kind == "commit_result":
            _, repo_name, ok, detail = msg
            rs = app.repos.get(repo_name)
            if not rs:
                continue
            if ok:
                rs.gen_status = GenStatus.IDLE
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                dpg.set_value(rs.status_tag, "Committed & pushed!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                # Re-poll to refresh
                executor.submit(bg_poll_repos)
            else:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = detail
                update_repo_status(rs)

        elif kind == "tray_show":
            # Restore window to center of screen
            dpg.set_viewport_pos([100, 100])

        elif kind == "tray_quit":
            dpg.stop_dearpygui()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="AI Commit Monitor GUI")
    parser.add_argument("folder", nargs="?", default=".",
                        help="Folder containing git repos to monitor")
    parser.add_argument("--model", default=os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
                        help="Ollama model name")
    parser.add_argument("--url", default=os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
                        help="Ollama base URL")
    parser.add_argument("--poll", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    return parser.parse_args()


green_btn_theme = None


def main():
    global green_btn_theme

    args = parse_args()
    app.watched_folder = Path(args.folder).resolve()
    app.model = args.model
    app.ollama_url = args.url
    app.poll_interval = args.poll

    dpg.create_context()

    # Viewport setup: compact, no OS title bar
    dpg.create_viewport(
        title="AI Commit Monitor",
        width=520,
        height=600,
        min_width=400,
        min_height=300,
        decorated=False,
        always_on_top=True,
    )

    # Theme
    global_theme = create_theme()
    dpg.bind_theme(global_theme)
    green_btn_theme = create_button_theme((50, 130, 75))

    # File dialog for folder selection
    with dpg.file_dialog(
        directory_selector=True,
        show=False,
        callback=cb_folder_selected,
        tag="folder_dialog",
        width=500,
        height=400,
    ):
        pass

    # Mouse handlers for drag-to-move
    with dpg.handler_registry():
        dpg.add_mouse_down_handler(button=dpg.mvMouseButton_Left, callback=mouse_down_handler)
        dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left, threshold=1, callback=mouse_drag_handler)
        dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left, callback=mouse_release_handler)

    # Main window
    with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                    no_move=True, no_close=True):

        # Custom title bar
        with dpg.group(horizontal=True):
            dpg.add_text("AI Commit Monitor", color=COL_ACCENT)
            dpg.add_spacer(width=-1)

        with dpg.group(horizontal=True):
            dpg.add_spacer(width=-1)
            dpg.add_button(label=" - ", callback=lambda: dpg.minimize_viewport(), width=30)
            dpg.add_button(label=" X ", callback=cb_close, width=30)

        dpg.add_separator()

        # Settings bar
        with dpg.group(horizontal=True):
            dpg.add_text("Watching:", color=COL_DIM)
            dpg.add_text(str(app.watched_folder), tag="folder_label")

        with dpg.group(horizontal=True):
            dpg.add_button(label="Browse", callback=cb_browse)
            dpg.add_button(label="Refresh", callback=cb_refresh)
            dpg.add_spacer(width=10)
            dpg.add_text("Poll:", color=COL_DIM)
            dpg.add_input_int(default_value=app.poll_interval, width=50,
                              min_value=5, min_clamped=True, max_value=600, max_clamped=True,
                              callback=cb_poll_changed, step=0)
            dpg.add_text("s", color=COL_DIM)
            dpg.add_spacer(width=10)
            dpg.add_checkbox(label="Auto-generate", default_value=app.auto_generate,
                             callback=cb_auto_generate)

        dpg.add_separator()

        # Scrollable repos container
        with dpg.child_window(tag="repos_container", autosize_x=True, autosize_y=True,
                              border=False):
            dpg.add_text("Scanning...", color=COL_DIM)

    dpg.set_primary_window("primary", True)

    dpg.setup_dearpygui()
    dpg.show_viewport()

    # System tray
    setup_tray()

    # Initial poll
    trigger_poll()

    # Render loop
    while dpg.is_dearpygui_running():
        process_queue()

        # Check if it's time to poll again
        now = time.time()
        if now - app.last_poll >= app.poll_interval:
            trigger_poll()

        dpg.render_dearpygui_frame()

    # Cleanup
    executor.shutdown(wait=False)
    if tray_icon:
        try:
            tray_icon.stop()
        except Exception:
            pass
    dpg.destroy_context()


if __name__ == "__main__":
    main()
