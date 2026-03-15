#!/usr/bin/env python3
"""AI Commit Monitor GUI — Dear PyGui desktop app for monitoring git repos."""

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-detach from console on Windows so the GUI runs independently.
# Pass --no-detach to keep it attached (useful for debugging).
# ---------------------------------------------------------------------------

_debug_mode = False

def _maybe_detach():
    global _debug_mode
    if sys.platform != "win32":
        return
    if "--no-detach" in sys.argv:
        sys.argv.remove("--no-detach")
        _debug_mode = True
        return
    if os.environ.get("_AI_COMMIT_GUI_CHILD"):
        return
    os.environ["_AI_COMMIT_GUI_CHILD"] = "1"
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen([pythonw] + sys.argv,
                     creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
    sys.exit(0)

_maybe_detach()

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
# Win32 API setup (Windows only) — declare argtypes so ctypes handles
# 64-bit HWND / pointer values correctly.
# ---------------------------------------------------------------------------

_user32 = None

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    _user32 = ctypes.windll.user32

    # SetWindowPos
    _user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,   # HWND hWnd
        ctypes.c_void_p,   # HWND hWndInsertAfter
        ctypes.c_int,      # int X
        ctypes.c_int,      # int Y
        ctypes.c_int,      # int cx
        ctypes.c_int,      # int cy
        ctypes.c_uint,     # UINT uFlags
    ]
    _user32.SetWindowPos.restype = ctypes.c_bool

    # ShowWindow
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool

    # SetForegroundWindow
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool

    # GetWindowRect
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.GetWindowRect.restype = ctypes.c_bool

    # GetCursorPos
    _user32.GetCursorPos.argtypes = [ctypes.c_void_p]
    _user32.GetCursorPos.restype = ctypes.c_bool

    # GetWindowLongW / SetWindowLongW (style manipulation)
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long

    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long

    # GetWindowThreadProcessId
    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)
    ]
    _user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

    # IsWindowVisible
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool

    # EnumWindows
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )
    _user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool


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


@dataclass
class AppState:
    watched_folder: Path = field(default_factory=lambda: Path("."))
    repos: dict = field(default_factory=dict)  # name -> RepoState
    poll_interval: int = 30
    auto_generate: bool = False
    always_on_top: bool = False
    model: str = "qwen3-coder:480b-cloud"
    ollama_url: str = "http://localhost:11434"
    last_poll: float = 0.0


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

app = AppState()
ui_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=4)
_hwnd = None  # Cached viewport HWND (Windows)

# Color palette
COL_BG = (30, 30, 35)
COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_YELLOW = (220, 180, 60)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)

# Height of the custom title bar drag region (pixels from top of window)
TITLEBAR_HEIGHT = 40

# Drag state: (cursor_start_x, cursor_start_y, win_start_x, win_start_y)
_drag_start = None


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

def _cache_hwnd():
    """Find and cache the viewport HWND using EnumWindows."""
    global _hwnd
    if sys.platform != "win32":
        return

    pid = os.getpid()
    candidates = []

    def _enum_cb(hwnd, _lparam):
        proc_id = ctypes.wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value == pid and _user32.IsWindowVisible(hwnd):
            candidates.append(hwnd)
        return True

    # prevent GC of the callback during EnumWindows
    cb = WNDENUMPROC(_enum_cb)
    _user32.EnumWindows(cb, None)
    if candidates:
        _hwnd = candidates[0]


def _add_resize_border():
    """Add WS_THICKFRAME so the frameless window gets resize handles."""
    if not _hwnd:
        return
    GWL_STYLE = -16
    WS_THICKFRAME = 0x00040000
    style = _user32.GetWindowLongW(_hwnd, GWL_STYLE)
    style |= WS_THICKFRAME
    _user32.SetWindowLongW(_hwnd, GWL_STYLE, style)
    # Force recalculation
    SWP_FRAMECHANGED = 0x0020
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    _user32.SetWindowPos(
        _hwnd, None, 0, 0, 0, 0,
        SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER,
    )


def _set_topmost(on_top):
    """Set or clear the TOPMOST flag via Win32."""
    if not _hwnd:
        return
    HWND_TOPMOST = ctypes.c_void_p(-1)
    HWND_NOTOPMOST = ctypes.c_void_p(-2)
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    flag = HWND_TOPMOST if on_top else HWND_NOTOPMOST
    ret = _user32.SetWindowPos(
        _hwnd, flag, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )
    if _debug_mode:
        print(f"[debug] SetWindowPos(topmost={on_top}) returned {ret}", flush=True)


def _hide_window():
    """Hide the viewport entirely (removes from taskbar too)."""
    if _hwnd:
        _user32.ShowWindow(_hwnd, 0)  # SW_HIDE


def _show_window():
    """Show the viewport and bring it to front."""
    if _hwnd:
        _user32.ShowWindow(_hwnd, 5)  # SW_SHOW
        _user32.SetForegroundWindow(_hwnd)
        if app.always_on_top:
            _set_topmost(True)


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
# Drag-to-move handlers (pure Win32 — no DPG coordinate functions)
# ---------------------------------------------------------------------------

def mouse_down_handler(sender, app_data):
    """On left-click in the title bar, record start positions for drag."""
    global _drag_start
    if sys.platform != "win32" or not _hwnd:
        return
    # Use DPG mouse pos ONLY to check if click is in title bar region
    mouse_pos = dpg.get_mouse_pos(local=False)
    if mouse_pos[1] >= TITLEBAR_HEIGHT:
        return
    # Don't drag if clicking title bar buttons
    try:
        if (dpg.is_item_hovered("minimize_btn") or
                dpg.is_item_hovered("close_btn")):
            return
    except Exception:
        pass
    # Capture cursor and window positions via Win32
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    rect = ctypes.wintypes.RECT()
    _user32.GetWindowRect(_hwnd, ctypes.byref(rect))
    _drag_start = (pt.x, pt.y, rect.left, rect.top)


def mouse_drag_handler(sender, app_data):
    """While dragging, move the window using Win32 SetWindowPos."""
    if _drag_start is None or not _hwnd:
        return
    pt = ctypes.wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    sx, sy, wx, wy = _drag_start
    new_x = wx + (pt.x - sx)
    new_y = wy + (pt.y - sy)
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    _user32.SetWindowPos(
        _hwnd, None, new_x, new_y, 0, 0,
        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
    )


def mouse_release_handler(sender, app_data):
    """Stop dragging on mouse release."""
    global _drag_start
    _drag_start = None


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


def cb_always_on_top(sender, app_data):
    app.always_on_top = dpg.get_value(sender)
    _set_topmost(app.always_on_top)


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


def cb_minimize(sender, app_data):
    dpg.minimize_viewport()


def cb_close(sender, app_data):
    """Hide to tray if available, otherwise quit."""
    if _has_tray and sys.platform == "win32":
        _hide_window()
    else:
        dpg.stop_dearpygui()


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

tray_icon = None
_has_tray = False


def setup_tray():
    global tray_icon, _has_tray
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return

    # Simple icon: blue rounded rect with white lines
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=10, fill=(100, 140, 230, 255))
    draw.rounded_rectangle([14, 18, 50, 26], radius=2, fill=(255, 255, 255, 200))
    draw.rounded_rectangle([14, 30, 42, 38], radius=2, fill=(255, 255, 255, 200))
    draw.rounded_rectangle([14, 42, 36, 50], radius=2, fill=(255, 255, 255, 200))

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
    _has_tray = True
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
                executor.submit(bg_poll_repos)
            else:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = detail
                update_repo_status(rs)

        elif kind == "tray_show":
            _show_window()

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
    parser.add_argument("--topmost", action="store_true",
                        help="Start with always-on-top enabled")
    parser.add_argument("--no-detach", action="store_true",
                        help="Keep attached to the launching terminal (for debugging)")
    return parser.parse_args()


green_btn_theme = None


def main():
    global green_btn_theme

    args = parse_args()
    app.watched_folder = Path(args.folder).resolve()
    app.model = args.model
    app.ollama_url = args.url
    app.poll_interval = args.poll
    app.always_on_top = args.topmost

    dpg.create_context()

    # Frameless viewport (no OS title bar — we draw our own)
    dpg.create_viewport(
        title="AI Commit Monitor",
        width=520,
        height=600,
        min_width=400,
        min_height=300,
        decorated=False,
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
        dpg.add_mouse_down_handler(button=dpg.mvMouseButton_Left,
                                   callback=mouse_down_handler)
        dpg.add_mouse_drag_handler(button=dpg.mvMouseButton_Left,
                                   callback=mouse_drag_handler, threshold=1)
        dpg.add_mouse_release_handler(button=dpg.mvMouseButton_Left,
                                      callback=mouse_release_handler)

    # Main window
    with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                    no_move=True, no_close=True):

        # Custom title bar (drag anywhere in this region to move)
        with dpg.group(horizontal=True):
            dpg.add_text("AI Commit Monitor", color=COL_ACCENT)
            dpg.add_spacer(width=-1)

        with dpg.group(horizontal=True):
            dpg.add_spacer(width=-1)
            dpg.add_button(label=" - ", callback=cb_minimize, width=30,
                           tag="minimize_btn")
            dpg.add_button(label=" X ", callback=cb_close, width=30,
                           tag="close_btn")

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

        with dpg.group(horizontal=True):
            dpg.add_checkbox(label="Auto-generate", default_value=app.auto_generate,
                             callback=cb_auto_generate)
            dpg.add_spacer(width=10)
            dpg.add_checkbox(label="Always on top", default_value=app.always_on_top,
                             callback=cb_always_on_top)

        dpg.add_separator()

        # Scrollable repos container
        with dpg.child_window(tag="repos_container", autosize_x=True, autosize_y=True,
                              border=False):
            dpg.add_text("Scanning...", color=COL_DIM)

    dpg.set_primary_window("primary", True)

    dpg.setup_dearpygui()
    dpg.show_viewport()

    # Let the window fully materialise before touching Win32 APIs
    _hwnd_ready = False
    if sys.platform == "win32":
        for _ in range(10):
            dpg.render_dearpygui_frame()
        _cache_hwnd()
        if _hwnd:
            _add_resize_border()
            if app.always_on_top:
                _set_topmost(True)
            _hwnd_ready = True
        if _debug_mode:
            print(f"[debug] HWND={_hwnd} ready={_hwnd_ready}", flush=True)

    # System tray
    setup_tray()

    # Initial poll
    trigger_poll()

    # Render loop
    _hwnd_retry_count = 0
    while dpg.is_dearpygui_running():
        process_queue()

        # Retry HWND detection if it failed at startup
        if sys.platform == "win32" and not _hwnd_ready and _hwnd_retry_count < 60:
            _cache_hwnd()
            if _hwnd:
                _add_resize_border()
                if app.always_on_top:
                    _set_topmost(True)
                _hwnd_ready = True
                if _debug_mode:
                    print(f"[debug] HWND={_hwnd} found on retry {_hwnd_retry_count}", flush=True)
            _hwnd_retry_count += 1

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
