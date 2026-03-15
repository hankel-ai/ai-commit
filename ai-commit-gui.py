#!/usr/bin/env python3
"""AI Commit Monitor GUI — Dear PyGui desktop app for monitoring git repos."""

import argparse
import json
import os
import queue
import subprocess
import sys
import textwrap
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

    # GetWindowThreadProcessId
    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.DWORD)
    ]
    _user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

    # IsWindowVisible
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool

    # IsIconic (True when window is minimized)
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool

    # GetWindowLongW
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long

    # SetWindowLongW
    _user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long

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
_window_hidden = False  # True when hidden to tray

# Color palette
COL_BG = (30, 30, 35)
COL_ACCENT = (100, 140, 230)
COL_GREEN = (80, 180, 100)
COL_RED = (220, 80, 80)
COL_YELLOW = (220, 180, 60)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)

_SETTINGS_FILE = Path(__file__).resolve().parent / "ai-commit-gui-settings.json"
_ICON_FILE = Path(__file__).resolve().parent / "ai-commit-icon.ico"
_DEFAULT_MODEL = "qwen3-coder:480b-cloud"


# ---------------------------------------------------------------------------
# Window settings persistence
# ---------------------------------------------------------------------------

def _load_settings():
    """Load saved window geometry. Returns dict or None."""
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _save_settings():
    """Save current viewport position, size, and app preferences."""
    try:
        pos = dpg.get_viewport_pos()
        data = {
            "x": int(pos[0]),
            "y": int(pos[1]),
            "width": dpg.get_viewport_width(),
            "height": dpg.get_viewport_height(),
            "auto_generate": app.auto_generate,
            "always_on_top": app.always_on_top,
            "poll_interval": app.poll_interval,
            "model": app.model,
            "watched_folder": str(app.watched_folder),
        }
        _SETTINGS_FILE.write_text(json.dumps(data))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Icon generation
# ---------------------------------------------------------------------------

_icon_image = None  # cached PIL Image for reuse by tray


def _generate_icon():
    """Create the app icon (.ico) using Pillow. Returns path string or empty."""
    global _icon_image
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ""

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Background: rounded blue square
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=(100, 140, 230, 255))
    # Git branch: vertical line with two commit dots
    draw.line([(32, 14), (32, 50)], fill=(255, 255, 255, 220), width=3)
    draw.ellipse([25, 12, 39, 26], fill=(255, 255, 255, 240))  # top commit
    draw.ellipse([25, 38, 39, 52], fill=(255, 255, 255, 240))  # bottom commit
    # Inner dots (the commit "holes")
    draw.ellipse([29, 16, 35, 22], fill=(100, 140, 230, 255))
    draw.ellipse([29, 42, 35, 48], fill=(100, 140, 230, 255))
    # Side branch line
    draw.line([(32, 20), (44, 30), (44, 38), (38, 44)], fill=(255, 255, 255, 200), width=2)

    _icon_image = img

    try:
        img.save(str(_ICON_FILE), format="ICO", sizes=[(64, 64), (32, 32), (16, 16)])
        return str(_ICON_FILE)
    except Exception:
        return ""


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

    cb = WNDENUMPROC(_enum_cb)
    _user32.EnumWindows(cb, None)
    if candidates:
        _hwnd = candidates[0]


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
    _user32.SetWindowPos(
        _hwnd, flag, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
    )


def _hide_taskbar_icon():
    """Remove the window from the taskbar using WS_EX_TOOLWINDOW.

    Also restores minimize/maximize buttons that WS_EX_TOOLWINDOW removes.
    """
    if not _hwnd:
        return
    GWL_EXSTYLE = -20
    GWL_STYLE = -16
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    # Hide from taskbar
    ex_style = _user32.GetWindowLongW(_hwnd, GWL_EXSTYLE)
    ex_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    _user32.SetWindowLongW(_hwnd, GWL_EXSTYLE, ex_style)
    # Restore minimize/maximize buttons
    style = _user32.GetWindowLongW(_hwnd, GWL_STYLE)
    style = style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
    _user32.SetWindowLongW(_hwnd, GWL_STYLE, style)


def _hide_window():
    """Hide the viewport entirely (removes from taskbar too)."""
    global _window_hidden
    if _hwnd:
        _user32.ShowWindow(_hwnd, 0)  # SW_HIDE
        _window_hidden = True


def _show_window():
    """Show the viewport and bring it to front."""
    global _window_hidden
    if _hwnd:
        _user32.ShowWindow(_hwnd, 5)  # SW_SHOW
        _user32.SetForegroundWindow(_hwnd)
        _window_hidden = False
        _set_tray_alert(False)  # clear indicator
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
            dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (30, 30, 35))
            dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (40, 42, 55))
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
# UI callbacks
# ---------------------------------------------------------------------------

def _native_folder_dialog(initial_dir):
    """Show native folder picker in a subprocess, return selected path or ''."""
    script = (
        "import tkinter as tk; from tkinter import filedialog; "
        "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
        f"p = filedialog.askdirectory(parent=root, initialdir={str(initial_dir)!r}); "
        "root.destroy(); print(p)"
    )
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, **kwargs,
    )
    return result.stdout.strip()


def bg_browse():
    """Run native folder picker in background, post result to UI queue."""
    chosen = _native_folder_dialog(app.watched_folder)
    if chosen:
        ui_queue.put(("folder_selected", chosen))


def cb_browse(sender, app_data):
    executor.submit(bg_browse)


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
    rs.commit_message = ""
    if rs.input_tag and dpg.does_item_exist(rs.input_tag):
        dpg.set_value(rs.input_tag, "")
    update_repo_status(rs)
    executor.submit(bg_generate_message, repo_name)


def cb_accept(sender, app_data, user_data):
    repo_name = user_data
    rs = app.repos.get(repo_name)
    if not rs:
        return
    widget_text = dpg.get_value(rs.input_tag).strip()
    if not widget_text:
        dpg.set_value(rs.status_tag, "No commit message.")
        dpg.configure_item(rs.status_tag, color=COL_RED)
        return
    # Use original unwrapped message if user hasn't edited the display text
    if rs.commit_message and widget_text == _wrap_for_display(rs.commit_message).strip():
        message = rs.commit_message
    else:
        message = widget_text
    rs.gen_status = GenStatus.GENERATING
    dpg.set_value(rs.status_tag, "Committing & pushing...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_commit_and_push, repo_name, message)



# ---------------------------------------------------------------------------
# Windows startup registry helpers
# ---------------------------------------------------------------------------

_STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_REG_NAME = "AICommitMonitor"


def _get_startup_command():
    """Return the command string to launch this app at startup."""
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    script = str(Path(__file__).resolve())
    return f'"{pythonw}" "{script}"'


def _is_startup_enabled():
    """Check if the app is registered to run at Windows startup."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, _STARTUP_REG_NAME)
        winreg.CloseKey(key)
        return True
    except (FileNotFoundError, OSError):
        return False


def _set_startup_enabled(enabled):
    """Add or remove the app from Windows startup registry."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, _STARTUP_REG_NAME, 0, winreg.REG_SZ, _get_startup_command())
        else:
            try:
                winreg.DeleteValue(key, _STARTUP_REG_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except OSError:
        pass


def cb_start_with_windows(sender, app_data):
    _set_startup_enabled(dpg.get_value(sender))


def cb_model_changed(sender, app_data):
    val = dpg.get_value(sender).strip()
    if val:
        app.model = val


def cb_model_reset(sender, app_data):
    app.model = _DEFAULT_MODEL
    dpg.set_value("model_input", _DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------

tray_icon = None
_has_tray = False
_tray_alert_active = False
_tray_icon_normal = None
_tray_icon_alert = None


def _make_alert_icon(base_img):
    """Return a copy of *base_img* with an orange dot in the top-right corner."""
    try:
        from PIL import ImageDraw
    except ImportError:
        return base_img
    img = base_img.copy()
    draw = ImageDraw.Draw(img)
    draw.ellipse([40, 2, 62, 24], fill=(255, 140, 0, 255))  # orange dot
    draw.ellipse([44, 6, 58, 20], fill=(255, 180, 40, 255))  # lighter center
    return img


def _set_tray_alert(on):
    """Toggle the tray icon between normal and alert (orange dot) variants."""
    global _tray_alert_active
    if not tray_icon or not _tray_icon_normal:
        return
    if on and not _tray_alert_active:
        tray_icon.icon = _tray_icon_alert or _tray_icon_normal
        tray_icon.title = "AI Commit Monitor — changes detected"
        _tray_alert_active = True
    elif not on and _tray_alert_active:
        tray_icon.icon = _tray_icon_normal
        tray_icon.title = "AI Commit Monitor"
        _tray_alert_active = False


def setup_tray():
    global tray_icon, _has_tray, _tray_icon_normal, _tray_icon_alert
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return

    if _icon_image:
        img = _icon_image.copy()
    else:
        img = Image.new("RGBA", (64, 64), (100, 140, 230, 255))

    _tray_icon_normal = img
    _tray_icon_alert = _make_alert_icon(img)

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
        display_text = _wrap_for_display(rs.commit_message) if rs.commit_message else ""
        input_h = _height_for_text(display_text) if display_text else 60
        rs.input_tag = dpg.add_input_text(
            default_value=display_text,
            hint="Commit message...",
            multiline=True,
            height=input_h,
            width=-1,
            tab_input=False,
            parent=rs.header_tag,
        )

        # Status line
        rs.status_tag = dpg.add_text("", parent=rs.header_tag)
        update_repo_status(rs)

        # Buttons row
        with dpg.group(horizontal=True, parent=rs.header_tag):
            rs.gen_btn_tag = dpg.add_button(label="Generate", callback=cb_generate, user_data=rs.name)
            rs.accept_btn_tag = dpg.add_button(label="Accept & Push", callback=cb_accept, user_data=rs.name)
            dpg.bind_item_theme(rs.accept_btn_tag, green_btn_theme)

        dpg.add_spacer(height=4, parent=rs.header_tag)
    else:
        rs.status_tag = dpg.add_text("Clean", color=COL_DIM, parent=rs.header_tag)
        rs.input_tag = 0



def _get_wrap_width():
    """Estimate how many characters fit in one line of the input widget."""
    try:
        vp_width = dpg.get_viewport_width()
    except Exception:
        vp_width = 520
    # Account for window padding, frame padding, scrollbar, collapsing header indent
    text_px = vp_width - 62
    char_px = 6.8  # DPG default proportional font average
    return max(40, int(text_px / char_px))


def _wrap_for_display(text):
    """Wrap text for display only. Does NOT modify the original commit message."""
    if not text:
        return text
    width = _get_wrap_width()
    out = []
    for line in text.split("\n"):
        if len(line) <= width:
            out.append(line)
        else:
            out.extend(textwrap.wrap(line, width=width,
                                     break_long_words=False,
                                     break_on_hyphens=False) or [""])
    return "\n".join(out)


def _height_for_text(text):
    """Return pixel height that fits *text* with no extra blank space."""
    if not text:
        return 60
    num_lines = text.count("\n") + 1
    # ~15px per line + frame padding
    return max(60, min(400, num_lines * 15 + 8))


def rebuild_repos_ui(results):
    """Rebuild repo sections from poll results.

    If a repo's file list changed since last poll, its pending commit message
    is erased (and auto-generated again if that setting is on).  If the files
    are unchanged, the existing message is preserved.
    """
    preserved = {}  # name -> (message, gen_status, error_message)
    for name, rs in app.repos.items():
        msg = ""
        if rs.input_tag and dpg.does_item_exist(rs.input_tag):
            msg = dpg.get_value(rs.input_tag)
        elif rs.commit_message:
            msg = rs.commit_message
        preserved[name] = (msg, rs.gen_status, rs.error_message)

    if dpg.does_item_exist("repos_container"):
        dpg.delete_item("repos_container", children_only=True)

    new_repos = {}
    any_changes = False
    for name, info in sorted(results.items(), key=lambda x: x[0].lower()):
        old_rs = app.repos.get(name)
        new_entries = info["entries"]

        # Detect whether files changed since last poll
        files_changed = True
        if old_rs is not None:
            files_changed = (old_rs.entries != new_entries)

        if new_entries:
            any_changes = True

        # Decide what to keep
        if files_changed or name not in preserved:
            msg = ""
            gen = GenStatus.IDLE
            err = ""
        else:
            prev_msg, prev_gen, prev_err = preserved[name]
            # Keep message only if still generating or files haven't changed
            if prev_gen == GenStatus.GENERATING:
                msg, gen, err = prev_msg, prev_gen, prev_err
            else:
                msg, gen, err = prev_msg, (GenStatus.DONE if prev_msg else GenStatus.IDLE), prev_err

        rs = RepoState(
            path=info["path"],
            name=name,
            entries=new_entries,
            commit_message=msg,
            gen_status=gen,
            error_message=err,
        )
        new_repos[name] = rs
        build_repo_section(rs, "repos_container")

    app.repos = new_repos

    # Auto-generate for repos with changes and no message
    for name, rs in app.repos.items():
        if rs.entries and not rs.commit_message and rs.gen_status == GenStatus.IDLE:
            if app.auto_generate:
                rs.gen_status = GenStatus.GENERATING
                update_repo_status(rs)
                executor.submit(bg_generate_message, name)

    # Notify tray if window is hidden and there are repos with changes
    if _window_hidden and any_changes:
        _set_tray_alert(True)


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
                    display = _wrap_for_display(message)
                    dpg.set_value(rs.input_tag, display)
                    dpg.configure_item(rs.input_tag, height=_height_for_text(display))
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

        elif kind == "folder_selected":
            chosen = msg[1]
            folder = Path(chosen)
            if folder.is_dir():
                app.watched_folder = folder
                dpg.set_value("folder_label", str(folder))
                trigger_poll()

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
    app.model = args.model
    app.ollama_url = args.url
    folder_from_cli = args.folder != "."

    dpg.create_context()

    # Load saved settings (CLI args override where specified)
    saved = _load_settings()
    vp_width = saved.get("width", 520) if saved else 520
    vp_height = saved.get("height", 600) if saved else 600

    # Restore preferences from disk, then let CLI flags override
    if saved:
        app.auto_generate = saved.get("auto_generate", False)
        app.always_on_top = saved.get("always_on_top", False)
        app.poll_interval = saved.get("poll_interval", 30)
        if "model" in saved:
            app.model = saved["model"]
        if not folder_from_cli and "watched_folder" in saved:
            p = Path(saved["watched_folder"])
            if p.is_dir():
                app.watched_folder = p

    # CLI folder argument takes priority over saved setting
    if folder_from_cli:
        app.watched_folder = Path(args.folder).resolve()
    elif not app.watched_folder or app.watched_folder == Path("."):
        app.watched_folder = Path(args.folder).resolve()
    if args.topmost:
        app.always_on_top = True
    if args.poll != 30:  # user explicitly passed --poll
        app.poll_interval = args.poll

    # Generate app icon
    icon_path = _generate_icon()

    vp_kwargs = {
        "title": "AI Commit Monitor",
        "width": vp_width,
        "height": vp_height,
        "min_width": 400,
        "min_height": 300,
        "decorated": True,
    }
    if icon_path:
        vp_kwargs["small_icon"] = icon_path
        vp_kwargs["large_icon"] = icon_path
    dpg.create_viewport(**vp_kwargs)

    # Theme
    global_theme = create_theme()
    dpg.bind_theme(global_theme)
    green_btn_theme = create_button_theme((50, 130, 75))

    # Main window
    with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                    no_move=True, no_close=True):

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
            dpg.add_spacer(width=10)
            dpg.add_checkbox(label="Run at startup", default_value=_is_startup_enabled(),
                             callback=cb_start_with_windows,
                             tag="startup_chk", show=(sys.platform == "win32"))

        dpg.add_separator()

        # Scrollable repos container (negative height reserves space for model bar)
        with dpg.child_window(tag="repos_container", autosize_x=True,
                              height=-35, border=False):
            dpg.add_text("Scanning...", color=COL_DIM)

        # Model bar at bottom
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_text("Model:", color=COL_DIM)
            dpg.add_input_text(tag="model_input", default_value=app.model,
                               width=-60, callback=cb_model_changed,
                               on_enter=True)
            dpg.add_button(label="Reset", callback=cb_model_reset)

    dpg.set_primary_window("primary", True)

    dpg.setup_dearpygui()
    dpg.show_viewport()

    # Restore saved position
    if saved and "x" in saved and "y" in saved:
        dpg.set_viewport_pos([saved["x"], saved["y"]])

    # Cache HWND for always-on-top and tray operations
    _hwnd_ready = False
    if sys.platform == "win32":
        for _ in range(10):
            dpg.render_dearpygui_frame()
        _cache_hwnd()
        if _hwnd:
            if app.always_on_top:
                _set_topmost(True)
            _hwnd_ready = True
        if _debug_mode:
            print(f"[debug] HWND={_hwnd} ready={_hwnd_ready}", flush=True)

    # System tray
    setup_tray()
    # Hide taskbar icon (app lives in the tray)
    if _hwnd_ready:
        _hide_taskbar_icon()

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
                if app.always_on_top:
                    _set_topmost(True)
                _hide_taskbar_icon()
                _hwnd_ready = True
            _hwnd_retry_count += 1

        # Intercept minimize → hide to tray instead
        if _hwnd and _has_tray and not _window_hidden and _user32.IsIconic(_hwnd):
            _user32.ShowWindow(_hwnd, 9)  # SW_RESTORE (undo iconic state)
            _hide_window()

        now = time.time()
        if now - app.last_poll >= app.poll_interval:
            trigger_poll()

        dpg.render_dearpygui_frame()

    # Save window geometry before cleanup
    _save_settings()

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
