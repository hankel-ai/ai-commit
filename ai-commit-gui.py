#!/usr/bin/env python3
"""AI Commit Monitor GUI -- Dear PyGui desktop app for monitoring git repos."""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# ---------------------------------------------------------------------------
# Auto-install missing dependencies from requirements.txt
# ---------------------------------------------------------------------------

def _ensure_dependencies():
    """Check for required packages and pip install them if missing."""
    required = {"dearpygui": "dearpygui", "pystray": "pystray", "PIL": "Pillow"}
    missing = []
    for import_name, pip_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"Installing missing dependencies: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet"] + missing,
        )

_ensure_dependencies()

import dearpygui.dearpygui as dpg

import webbrowser

import activity_log
from ai_commit_core import (
    STATUS_LABELS,
    KiroCliError,
    OllamaError,
    compute_header_open,
    default_config,
    discover_repos,
    find_autostash_ref,
    do_commit_and_push,
    describe_empty_diff,
    do_pull,
    is_secret_push_block,
    SECRET_PUSH_SKIP_OPTION,
    generate_message,
    get_active_github_account,
    get_branch_classification,
    get_current_branch,
    get_diff,
    get_github_account,
    get_head_sha,
    get_incoming_changes,
    get_repo_visibility,
    get_last_commit,
    is_repo_active,
    is_folder_recent,
    get_remote_url,
    get_status,
    read_status,
    read_status_branch,
    fetch_remote,
    is_git_repo,
    run_git,
)

import chime
from gh_workflows import (
    detect_runs_for_commit,
    fetch_workflow_yaml,
    get_gh_token,
    has_workflow_dispatch,
    parse_owner_repo,
)

# ---------------------------------------------------------------------------
# Win32 API setup (Windows only) -- declare argtypes so ctypes handles
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

    # CreateWindowExW
    _user32.CreateWindowExW.argtypes = [
        ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.CreateWindowExW.restype = ctypes.c_void_p

    # SetWindowLongPtrW (pointer-width variant for 64-bit HWND values)
    _user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p

    # EnumWindows
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )
    _user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool

    # DwmSetWindowAttribute -- dark title bar
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p,   # HWND
        ctypes.c_ulong,    # DWORD dwAttribute
        ctypes.c_void_p,   # LPCVOID pvAttribute
        ctypes.c_ulong,    # DWORD cbAttribute
    ]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

    # CallWindowProcW -- used to chain to the original WNDPROC after subclassing
    _user32.CallWindowProcW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    _user32.CallWindowProcW.restype = ctypes.c_long

    # shell32 drag-and-drop
    _shell32 = ctypes.windll.shell32
    _shell32.DragAcceptFiles.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    _shell32.DragAcceptFiles.restype = None
    _shell32.DragQueryFileW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint,
    ]
    _shell32.DragQueryFileW.restype = ctypes.c_uint
    _shell32.DragFinish.argtypes = [ctypes.c_void_p]
    _shell32.DragFinish.restype = None

    WNDPROC_TYPE = ctypes.WINFUNCTYPE(
        ctypes.c_long,        # LRESULT
        ctypes.c_void_p,      # HWND
        ctypes.c_uint,        # UINT msg
        ctypes.c_void_p,      # WPARAM
        ctypes.c_void_p,      # LPARAM
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class GenStatus(Enum):
    IDLE = auto()
    GENERATING = auto()
    DONE = auto()
    ERROR = auto()


def _repo_name_from_url(remote_url):
    """Extract the repository name from a git remote URL."""
    if not remote_url:
        return ""
    # Strip trailing slashes and .git
    url = remote_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # Get the last path component
    return url.rsplit("/", 1)[-1] if "/" in url else ""


@dataclass
class RepoState:
    path: Path
    name: str  # display name: git repo name if available, else folder name
    folder_name: str  # actual folder name on disk
    entries: list  # list of (status_code, filepath)
    diff: str = ""
    commit_message: str = ""
    gen_status: GenStatus = GenStatus.IDLE
    error_message: str = ""
    remote_url: str = ""
    github_account: str = ""
    visibility: str = ""
    branch: str = ""
    branch_status: str = ""  # "", "local only", or "stale"
    last_commit_msg: str = ""
    last_commit_date: str = ""
    last_commit_ts: float = 0.0
    ahead: int = 0
    behind: int = 0
    gen_entries: list = field(default_factory=list)
    # dpg widget tags
    header_tag: int = 0
    files_group_tag: int = 0
    more_group_tag: int = 0
    input_tag: int = 0
    status_tag: int = 0
    gen_btn_tag: int = 0
    accept_btn_tag: int = 0


@dataclass
class NonGitFolder:
    path: Path
    name: str
    mtime: float = 0.0  # folder modification time (recency filter); 0.0 = unknown
    header_tag: int = 0
    status_tag: int = 0


@dataclass
class AppState:
    watched_folders: list = field(default_factory=list)  # list of Path
    repos: dict = field(default_factory=dict)  # repo_key -> RepoState
    poll_interval: int = 30
    auto_generate: bool = False
    always_on_top: bool = False
    model: str = "qwen3-coder:480b-cloud"
    provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    last_poll: float = 0.0
    paused: bool = False
    actions_popup_enabled: bool = True
    chime_on_completion: bool = False
    show_non_git_folders: bool = True
    sort_by_date: bool = False  # True = newest-first; False = alphabetical
    recent_only: bool = True  # hide idle repos (old + clean) from the list
    recent_days: int = 14  # a commit within this many days counts as "recent"
    idle_poll_interval: int = 900  # seconds between polls of idle-tier repos
    idle_last_poll: dict = field(default_factory=dict)  # repo_key -> last idle-poll epoch (transient)
    last_results: dict = field(default_factory=dict)  # last poll_result payload (for no-poll re-render)
    last_non_git: dict = field(default_factory=dict)  # last non-git payload (for no-poll re-render)
    active_gh_account: str = ""
    non_git_folders: dict = field(default_factory=dict)
    repo_overrides: dict = field(default_factory=dict)  # repo_key -> "pause" or "active"
    expand_on_next_build: set = field(default_factory=set)  # repo_keys to auto-expand on next UI rebuild (used when paused + single-repo Refresh)
    collapse_on_next_build: set = field(default_factory=set)  # repo_keys to re-apply the activity default (collapse if idle) on next UI rebuild, overriding preserve_open (used after Commit & Push)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

app = AppState()
ui_queue = queue.Queue()
executor = ThreadPoolExecutor(max_workers=4)
_hwnd = None  # Cached viewport HWND (Windows)
_nswindow = None  # Cached NSWindow (macOS)
_pending_topmost = None  # Deferred macOS topmost change (True/False/None)
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
_LOCK_FILE = Path(tempfile.gettempdir()) / ".ai-commit-gui.lock"
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
            "provider": app.provider,
            "ollama_url": app.ollama_url,
            "watched_folders": [str(f) for f in app.watched_folders],
            "actions_popup_enabled": app.actions_popup_enabled,
            "chime_on_completion": app.chime_on_completion,
            "show_non_git_folders": app.show_non_git_folders,
            "recent_only": app.recent_only,
            "recent_days": app.recent_days,
            "idle_poll_interval": app.idle_poll_interval,
            "repo_overrides": app.repo_overrides,
            "sort_by_date": app.sort_by_date,
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
# Platform window helpers
# ---------------------------------------------------------------------------

def _cache_nswindow():
    """Find and cache the viewport NSWindow on macOS."""
    global _nswindow
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication
        for win in NSApplication.sharedApplication().windows():
            try:
                if win.title() == "AI Commit Monitor":
                    _nswindow = win
                    return
            except Exception:
                continue
    except Exception:
        pass


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
        _set_dark_title_bar()


def _set_dark_title_bar():
    """Enable the immersive dark-mode title bar via DwmSetWindowAttribute."""
    if not _hwnd or sys.platform != "win32":
        return
    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    value = ctypes.c_int(1)
    _dwmapi.DwmSetWindowAttribute(
        _hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
        ctypes.byref(value), ctypes.sizeof(value),
    )


# Drag-and-drop: subclass the viewport WNDPROC to handle WM_DROPFILES.
# Strong refs prevent the WNDPROC callback from being garbage-collected
# while Windows still holds a pointer to it.
_drop_orig_wndproc = None
_drop_wndproc_ref = None
_drop_installed = False


def _drop_wndproc(hwnd, msg, wparam, lparam):
    WM_DROPFILES = 0x0233
    if msg == WM_DROPFILES:
        try:
            hdrop = wparam
            count = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
            for i in range(count):
                length = _shell32.DragQueryFileW(hdrop, i, None, 0)
                buf = ctypes.create_unicode_buffer(length + 1)
                _shell32.DragQueryFileW(hdrop, i, buf, length + 1)
                path = buf.value
                if path:
                    ui_queue.put(("folder_selected", path))
            _shell32.DragFinish(hdrop)
        except Exception:
            pass
        return 0
    return _user32.CallWindowProcW(_drop_orig_wndproc, hwnd, msg, wparam, lparam)


def _install_drop_target():
    """Register the viewport for OS folder/file drops (Windows only)."""
    global _drop_orig_wndproc, _drop_wndproc_ref, _drop_installed
    if _drop_installed or not _hwnd or sys.platform != "win32":
        return
    GWLP_WNDPROC = -4
    _drop_wndproc_ref = WNDPROC_TYPE(_drop_wndproc)
    new_ptr = ctypes.cast(_drop_wndproc_ref, ctypes.c_void_p)
    _drop_orig_wndproc = _user32.SetWindowLongPtrW(_hwnd, GWLP_WNDPROC, new_ptr)
    _shell32.DragAcceptFiles(_hwnd, True)
    _drop_installed = True


def _set_topmost(on_top):
    """Set or clear the always-on-top flag (cross-platform)."""
    if sys.platform == "win32":
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
    elif sys.platform == "darwin":
        # Defer to run between render frames -- calling setLevel_ during a
        # DPG/GLFW render callback causes a SIGTRAP crash.
        global _pending_topmost
        _pending_topmost = on_top


_hidden_owner_hwnd = None


def _hide_taskbar_icon():
    """Remove the window from the taskbar by giving it a hidden owner window.

    A top-level window with an owner does not appear in the taskbar.
    This avoids WS_EX_TOOLWINDOW which shrinks the title bar.
    """
    global _hidden_owner_hwnd
    if not _hwnd:
        return
    # Create a tiny hidden window to act as owner
    WS_POPUP = 0x80000000
    _hidden_owner_hwnd = _user32.CreateWindowExW(
        0, "Static", None, WS_POPUP,
        0, 0, 0, 0,
        None, None, None, None,
    )
    # Setting GWLP_HWNDPARENT on a top-level window sets its *owner*
    GWLP_HWNDPARENT = -8
    _user32.SetWindowLongPtrW(_hwnd, GWLP_HWNDPARENT, _hidden_owner_hwnd)


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

def _cached_repo_result(rp, existing):
    """Reuse a repo's last-known data when we intentionally skip re-polling it."""
    return {
        "path": rp,
        "entries": existing.entries,
        "remote_url": existing.remote_url,
        "github_account": existing.github_account,
        "visibility": existing.visibility,
        "branch": existing.branch,
        "branch_status": existing.branch_status,
        "last_commit_msg": existing.last_commit_msg,
        "last_commit_date": existing.last_commit_date,
        "last_commit_ts": existing.last_commit_ts,
        "ahead": existing.ahead,
        "behind": existing.behind,
    }


def _read_poll_status(rp, remote_url, do_fetch):
    """Folded git poll: one ``git status --porcelain --branch`` (after an
    optional fetch) yields dirty entries, current branch, ahead/behind and the
    current branch's classification.

    Replaces the old get_status + get_current_branch + get_sync_status +
    get_branch_classification sequence (4 git spawns -> 1, plus the fetch). See
    docs/polling-performance.md. Returns
    ``(entries, branch, ahead, behind, branch_status)``.
    """
    if do_fetch:
        fetch_remote(rp)
    _ok, entries, info = read_status_branch(rp)
    branch_status = info["classification"]
    # Preserve old behavior: suppress the per-branch "local only" badge for
    # repos with no remote at all (the header already shows LOCAL). Repos that
    # DO have a remote but an untracked branch still show "local only".
    if branch_status == "local only" and not remote_url:
        branch_status = ""
    return entries, info["branch"], info["ahead"], info["behind"], branch_status


def _poll_one_repo(rp, existing, repo_force, force):
    """Run a live git poll for a single repo and return its result dict."""
    ui_queue.put(("repo_loading", str(rp), rp.name))
    last_msg, last_date, last_ts = get_last_commit(rp)
    is_new = existing is None
    if not repo_force and existing and existing.remote_url:
        remote_url = existing.remote_url
    else:
        remote_url = get_remote_url(rp)
    github_account = get_github_account(remote_url)
    if not repo_force and existing and existing.visibility:
        visibility = existing.visibility
    else:
        visibility = get_repo_visibility(rp) if remote_url else ""
    entries, branch, ahead, behind, branch_status = _read_poll_status(
        rp, remote_url, do_fetch=is_new or repo_force)
    return {
        "path": rp,
        "entries": entries,
        "remote_url": remote_url,
        "github_account": github_account,
        "visibility": visibility,
        "branch": branch,
        "branch_status": branch_status,
        "last_commit_msg": last_msg,
        "last_commit_date": last_date,
        "last_commit_ts": last_ts,
        "ahead": ahead,
        "behind": behind,
    }


def bg_poll_repos(force=False):
    """Discover repos and get status for each. Posts results to ui_queue.

    When *force* is True, bypass cached remote_url and always run a
    network fetch -- same behavior as a fresh startup. Used by the manual
    Refresh button so a moved remote is picked up without restarting.
    """
    active_account = get_active_github_account()
    ui_queue.put(("active_gh_account", active_account))

    now = time.time()
    results = {}
    non_git_results = {}

    # While globally paused (and not a manual Refresh), skip the folder rescan --
    # it runs `git rev-parse` on every repo just to rediscover them. Poll only
    # the force-active repos from the already-known set and reuse cached data for
    # the rest so they stay in the UI. New repos surface on Unpause or Refresh.
    if app.paused and not force:
        for repo_key, existing in list(app.repos.items()):
            if app.repo_overrides.get(repo_key, "") == "active":
                results[repo_key] = _poll_one_repo(existing.path, existing, True, force)
            else:
                results[repo_key] = _cached_repo_result(existing.path, existing)
        ui_queue.put(("poll_result", results, _non_git_for_rebuild(), force))
        return

    for folder in app.watched_folders:
        folder_path = Path(folder).resolve()
        if not folder_path.is_dir():
            continue
        repo_paths = []
        non_git_paths = []
        parent_is_repo = is_git_repo(folder_path)
        if parent_is_repo:
            repo_paths.append(folder_path)
        candidate_non_git = []
        git_child_count = 0
        for child in sorted(folder_path.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if is_git_repo(child):
                    repo_paths.append(child)
                    git_child_count += 1
                else:
                    candidate_non_git.append(child)
        if not parent_is_repo and git_child_count == 0:
            # Watched folder itself isn't git and has no git children -- surface
            # the folder itself so it can be Init'd. Don't list its subfolders;
            # they'd just become part of that single new repo.
            non_git_paths.append(folder_path)
        elif (not parent_is_repo) or git_child_count > 0:
            non_git_paths.extend(candidate_non_git)
        for rp in repo_paths:
            repo_key = str(rp)
            repo_override = app.repo_overrides.get(repo_key, "")
            existing = app.repos.get(repo_key)
            skip_poll = (repo_override == "pause"
                         or (not force and app.paused and repo_override != "active"))
            if skip_poll and existing:
                results[repo_key] = _cached_repo_result(rp, existing)
                continue
            # Idle tier: a known repo that isn't recent/active (clean, synced, last
            # commit older than recent_days) is polled only on the slow idle
            # cadence -- the CPU win for the many rarely-touched repos. New repos,
            # manual Refresh (force), and force-active overrides always poll live.
            # idle_last_poll holds each repo's last *live* poll time (stamped below
            # on every real poll), so a repo just polled as new/active isn't
            # immediately re-polled the cycle it's reclassified idle.
            if (existing and not force and repo_override != "active"
                    and not is_repo_active(existing.last_commit_ts,
                                           bool(existing.entries),
                                           existing.ahead, existing.behind,
                                           now, app.recent_days)
                    and now - app.idle_last_poll.get(repo_key, 0) < app.idle_poll_interval):
                results[repo_key] = _cached_repo_result(rp, existing)
                continue
            repo_force = force or repo_override == "active"
            results[repo_key] = _poll_one_repo(rp, existing, repo_force, force)
            app.idle_last_poll[repo_key] = now
        for ngp in non_git_paths:
            ng_key = str(ngp)
            try:
                ng_mtime = ngp.stat().st_mtime
            except OSError:
                ng_mtime = 0.0
            non_git_results[ng_key] = {"path": ngp, "name": ngp.name,
                                       "mtime": ng_mtime}
    ui_queue.put(("poll_result", results, non_git_results, force))


def bg_refresh_single_repo(repo_name, force=False):
    """Re-poll a single repo and post its updated info to ui_queue.

    When *force* is True, bypass cached remote_url/visibility and
    always run git fetch -- same as the initial load or manual Refresh.
    """
    rs = app.repos.get(repo_name)
    if not rs:
        return
    rp = rs.path
    ui_queue.put(("repo_loading", repo_name, rp.name))
    last_msg, last_date, last_ts = get_last_commit(rp)
    if force:
        remote_url = get_remote_url(rp)
    else:
        remote_url = rs.remote_url or get_remote_url(rp)
    github_account = get_github_account(remote_url)
    if force:
        visibility = get_repo_visibility(rp) if remote_url else ""
    else:
        visibility = rs.visibility or (get_repo_visibility(rp) if remote_url else "")
    entries, branch, ahead, behind, branch_status = _read_poll_status(
        rp, remote_url, do_fetch=True)
    ui_queue.put(("single_repo_refresh", repo_name, {
        "path": rp,
        "entries": entries,
        "remote_url": remote_url,
        "github_account": github_account,
        "visibility": visibility,
        "branch": branch,
        "branch_status": branch_status,
        "last_commit_msg": last_msg,
        "last_commit_date": last_date,
        "last_commit_ts": last_ts,
        "ahead": ahead,
        "behind": behind,
    }, force))


def bg_generate_message(repo_name):
    """Generate commit message for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        diff = get_diff(rs.path)
        if not diff.strip():
            # An empty diff on a repo git still calls dirty is almost always
            # EOL normalization -- say so instead of a bare "no diff".
            detail = describe_empty_diff(rs.path) or "No diff content available."
            ui_queue.put(("gen_result", repo_name, "", detail))
            return
        config = {"provider": app.provider, "model": app.model, "url": app.ollama_url}
        activity_log.log_event(
            f"Generate commit message ({app.provider}/{app.model})",
            repo=repo_name, category=activity_log.CAT_AI,
        )
        msg = generate_message(diff, config)
        ui_queue.put(("gen_result", repo_name, msg, ""))
    except (OllamaError, KiroCliError) as exc:
        ui_queue.put(("gen_result", repo_name, "", str(exc)))
    except Exception as exc:
        ui_queue.put(("gen_result", repo_name, "", f"Unexpected error: {exc}"))


def bg_pull(repo_name):
    """Pull latest changes for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        activity_log.log_event("Pull", repo=repo_name)
        ok, detail = do_pull(rs.path)
        ui_queue.put(("pull_result", repo_name, ok, detail))
    except Exception as exc:
        ui_queue.put(("pull_result", repo_name, False, str(exc)))


def bg_preview_pull(repo_name):
    """Fetch incoming changes for preview. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        commits, diffstat = get_incoming_changes(rs.path)
        ui_queue.put(("preview_pull_result", repo_name, commits, diffstat))
    except Exception as exc:
        ui_queue.put(("preview_pull_result", repo_name, "", str(exc)))


def _launch_activity_viewer():
    """Open the standalone activity-log viewer window (separate OS process).

    The viewer tails the shared JSONL log file, so multiple things the app does
    show up live. Mirrors the gh_workflow_viewer launch pattern (pythonw on
    Windows so no console window flashes).
    """
    viewer = str(Path(__file__).resolve().parent / "activity_log_viewer.py")
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        pw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(pw):
            exe = pw
    try:
        subprocess.Popen([exe, viewer, str(activity_log.LOG_PATH)])
        activity_log.log_event("Opened activity log viewer")
    except Exception as exc:
        activity_log.log_event(
            "Failed to open activity log viewer", detail=str(exc),
            category=activity_log.CAT_ERROR,
        )


def cb_open_activity_log(sender=None, app_data=None):
    _launch_activity_viewer()


def _spawn_viewer_process(payload):
    """Launch gh_workflow_viewer.py, piping the payload JSON via stdin.

    The payload contains the gh auth token, so it must never touch disk -- a
    temp file would outlive us if the viewer failed to start.
    """
    viewer = str(Path(__file__).resolve().parent / "gh_workflow_viewer.py")
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        pw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(pw):
            exe = pw
    proc = subprocess.Popen(
        [exe, viewer, "-"],
        stdin=subprocess.PIPE,
    )
    proc.stdin.write(json.dumps(payload).encode("utf-8"))
    proc.stdin.close()


def _launch_workflow_viewer(repo_name, rs):
    """Check for workflow runs, then launch viewer only if any exist.

    Runs in background thread -- blocks during detection polling.
    Posts a workflow_check status to ui_queue so the GUI can surface
    silent failure modes (no gh token, no runs triggered, etc).
    """
    token = get_gh_token()
    if not token:
        ui_queue.put(("workflow_check", repo_name, "no_token"))
        return
    owner, repo = parse_owner_repo(rs.remote_url)
    sha = get_head_sha(str(rs.path))
    if not owner or not repo or not sha:
        ui_queue.put(("workflow_check", repo_name, "no_remote"))
        return

    runs = detect_runs_for_commit(owner, repo, sha, token, timeout=30)
    if not runs:
        ui_queue.put(("workflow_check", repo_name, "no_runs"))
        return

    _spawn_viewer_process({
        "owner": owner, "repo": repo, "sha": sha, "token": token,
        "chime_enabled": app.chime_on_completion,
    })


def _launch_workflow_viewer_dispatch(repo_name, wf_id, wf_name, after_iso):
    """Launch viewer in dispatch mode for a freshly dispatched workflow."""
    rs = app.repos.get(repo_name)
    if not rs or not rs.remote_url:
        return
    token = get_gh_token()
    if not token:
        return
    owner, repo = parse_owner_repo(rs.remote_url)
    if not owner or not repo:
        return

    _spawn_viewer_process({
        "owner": owner, "repo": repo, "token": token,
        "workflow_id": wf_id, "workflow_name": wf_name,
        "after_iso": after_iso,
        "chime_enabled": app.chime_on_completion,
    })


def bg_commit_and_push(repo_name, message):
    """Commit and push for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        if rs.gen_entries:
            current = get_status(rs.path)
            if current != rs.gen_entries:
                ui_queue.put(("commit_result", repo_name, False, False, "STALE"))
                return
        activity_log.log_event("Commit and push", repo=repo_name)
        committed, pushed, detail = do_commit_and_push(rs.path, message)
        ui_queue.put(("commit_result", repo_name, committed, pushed, detail))
    except Exception as exc:
        ui_queue.put(("commit_result", repo_name, False, False, str(exc)))


def bg_push_set_upstream(repo_name, branch):
    """Push with --set-upstream for a branch that has no remote tracking."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    rc, out, err = run_git(["push", "--set-upstream", "origin", branch],
                           cwd=str(rs.path))
    if rc == 0:
        ui_queue.put(("push_upstream_result", repo_name, True, out.strip()))
    else:
        # Carry the branch so a secret-push-protection block can offer an
        # override retry that still sets the upstream.
        ui_queue.put(("push_upstream_result", repo_name, False, err.strip(),
                      branch))


def bg_push_override(repo_name, branch=""):
    """Re-push, skipping GitLab secret push protection for this one push.

    Only invoked after the user confirms the override prompt for a push that
    was blocked by the secret-detection pre-receive hook. When *branch* is
    set, the blocked push was a --set-upstream push, so retry with it too.
    Posts push_upstream_result -- the success path there (status, collapse,
    refresh, Actions viewer) is exactly what a completed push needs.
    """
    rs = app.repos.get(repo_name)
    if not rs:
        return
    args = ["push", "-o", SECRET_PUSH_SKIP_OPTION]
    if branch:
        args += ["--set-upstream", "origin", branch]
    activity_log.log_event(
        f"Push with -o {SECRET_PUSH_SKIP_OPTION} (user override)",
        repo=repo_name, category=activity_log.CAT_GIT,
    )
    rc, out, err = run_git(args, cwd=str(rs.path))
    if rc == 0:
        ui_queue.put(("push_upstream_result", repo_name, True, out.strip()))
    else:
        ui_queue.put(("push_upstream_result", repo_name, False, err.strip(),
                      branch))


def bg_refresh_then_generate(repo_name):
    """Refresh repo status then generate a commit message.

    Posts a single_repo_refresh first, then proceeds to generate.
    """
    rs = app.repos.get(repo_name)
    if not rs:
        return
    rp = rs.path
    last_msg, last_date, last_ts = get_last_commit(rp)
    remote_url = rs.remote_url or get_remote_url(rp)
    github_account = get_github_account(remote_url)
    visibility = rs.visibility or (get_repo_visibility(rp) if remote_url else "")
    entries, branch, ahead, behind, branch_status = _read_poll_status(
        rp, remote_url, do_fetch=False)
    ui_queue.put(("refresh_then_generate", repo_name, {
        "path": rp,
        "entries": entries,
        "remote_url": remote_url,
        "github_account": github_account,
        "visibility": visibility,
        "branch": branch,
        "branch_status": branch_status,
        "last_commit_msg": last_msg,
        "last_commit_date": last_date,
        "last_commit_ts": last_ts,
        "ahead": ahead,
        "behind": behind,
    }))


def bg_create_remote(repo_name, account, visibility):
    """Create a GitHub repo under the given account and push.

    Args:
        repo_name: repo key (path string)
        account: GitHub login to own the new repo
        visibility: "private" or "public"
    """
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        cwd = str(rs.path)
        folder_name = rs.path.name
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        # Detect currently active account so we can restore it afterwards.
        original_account = None
        try:
            detect = subprocess.run(
                ["gh", "auth", "status", "--active"],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=10, **kwargs,
            )
            if detect.returncode == 0:
                for line in detect.stdout.splitlines() + detect.stderr.splitlines():
                    if "Logged in" in line and " account " in line:
                        original_account = line.split(" account ")[1].split()[0].strip()
                        break
        except Exception:
            pass

        # Switch to the target account if it differs from the active one.
        if account and account != original_account:
            switch = subprocess.run(
                ["gh", "auth", "switch", "--user", account],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, **kwargs,
            )
            if switch.returncode != 0:
                err = switch.stderr.strip() or switch.stdout.strip()
                ui_queue.put(("create_remote_result", repo_name, False,
                              f"Failed to switch to account '{account}': {err}"))
                return

        vis_flag = f"--{visibility}"
        result = subprocess.run(
            ["gh", "repo", "create", f"{account}/{folder_name}",
             vis_flag, "--source", cwd, "--push"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            **kwargs,
        )

        # Restore the original active account (best-effort).
        if original_account and account != original_account:
            try:
                subprocess.run(
                    ["gh", "auth", "switch", "--user", original_account],
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=15, **kwargs,
                )
            except Exception:
                pass

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            ui_queue.put(("create_remote_result", repo_name, False, err))
        else:
            remote_url = get_remote_url(cwd)
            ui_queue.put(("create_remote_result", repo_name, True, remote_url))
    except FileNotFoundError:
        ui_queue.put(("create_remote_result", repo_name, False,
                       "gh CLI not found. Install from https://cli.github.com"))
    except subprocess.TimeoutExpired:
        ui_queue.put(("create_remote_result", repo_name, False,
                       "gh repo create timed out after 60 seconds."))
    except Exception as exc:
        ui_queue.put(("create_remote_result", repo_name, False, str(exc)))


def bg_detect_gh_accounts(repo_key, click_pos=(0, 0)):
    """Detect authenticated GitHub accounts. Posts result to ui_queue."""
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=10, **kwargs,
        )
        # gh auth status output: "Logged in to github.com account <login> (keyring)"
        output = result.stdout + "\n" + result.stderr
        accounts = []
        active = ""
        for line in output.splitlines():
            if "Logged in" in line and " account " in line:
                login = line.split(" account ")[1].split()[0].strip()
                accounts.append(login)
            if "Active account" in line and "true" in line.lower():
                if accounts:
                    active = accounts[-1]
        if not active and accounts:
            active = accounts[0]
        ui_queue.put(("gh_accounts_result", repo_key, accounts, active, click_pos))
    except FileNotFoundError:
        ui_queue.put(("gh_accounts_result", repo_key, [], "", click_pos))
    except Exception:
        ui_queue.put(("gh_accounts_result", repo_key, [], "", click_pos))


# ---------------------------------------------------------------------------
# UI callbacks
# ---------------------------------------------------------------------------

def _native_folder_dialog(initial_dir):
    """Show native folder picker, return selected path or ''."""
    if sys.platform == "darwin":
        return _native_folder_dialog_macos(initial_dir)
    # Windows / Linux: use tkinter in a subprocess
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


def _native_folder_dialog_macos(initial_dir):
    """Show native NSOpenPanel folder picker on macOS via subprocess.

    NSOpenPanel must run on the main thread of its process, so we spawn a
    small helper that owns the Cocoa event loop.
    """
    script = (
        "from AppKit import NSOpenPanel, NSURL, NSApplication; "
        "NSApplication.sharedApplication().setActivationPolicy_(0); "
        "panel = NSOpenPanel.openPanel(); "
        "panel.setCanChooseFiles_(False); "
        "panel.setCanChooseDirectories_(True); "
        "panel.setAllowsMultipleSelection_(False); "
        f"panel.setDirectoryURL_(NSURL.fileURLWithPath_({str(initial_dir)!r})); "
        "ret = panel.runModal(); "
        "print(str(panel.URL().path()) if ret == 1 else '')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def bg_browse():
    """Run native folder picker in background, post result to UI queue."""
    initial = app.watched_folders[-1] if app.watched_folders else Path(".")
    chosen = _native_folder_dialog(initial)
    if chosen:
        ui_queue.put(("folder_selected", chosen))


def cb_browse(sender, app_data):
    executor.submit(bg_browse)


def cb_refresh(sender, app_data):
    # Manual Refresh = forced poll: re-read remote_url and fetch,
    # matching what startup does. Otherwise a moved remote stays cached.
    trigger_poll(force=True)


def cb_repo_right_click(sender, app_data, user_data):
    """Show a context menu when a repo header is right-clicked."""
    repo_key = user_data
    menu_tag = "repo_context_menu"
    if dpg.does_item_exist(menu_tag):
        dpg.delete_item(menu_tag)
    mx, my = dpg.get_mouse_pos(local=False)
    current = app.repo_overrides.get(repo_key, "")
    pause_label = "Remove Force Paused" if current == "pause" else "Force Paused"
    active_label = "Remove Force Active" if current == "active" else "Force Active"
    with dpg.window(tag=menu_tag, no_title_bar=True, popup=True,
                    pos=(int(mx), int(my)), no_move=True, no_resize=True,
                    min_size=(1, 1), max_size=(300, 200)):
        dpg.add_button(label="Refresh", width=160,
                       callback=_ctx_refresh_repo, user_data=repo_key)
        dpg.add_button(label=pause_label, width=160,
                       callback=_ctx_toggle_force, user_data=(repo_key, "pause"))
        dpg.add_button(label=active_label, width=160,
                       callback=_ctx_toggle_force, user_data=(repo_key, "active"))


def _ctx_toggle_force(sender, app_data, user_data):
    """Context-menu action: toggle force-pause or force-active for a repo."""
    repo_key, mode = user_data
    if dpg.does_item_exist("repo_context_menu"):
        dpg.delete_item("repo_context_menu")
    current = app.repo_overrides.get(repo_key, "")
    if current == mode:
        app.repo_overrides.pop(repo_key, None)
    else:
        app.repo_overrides[repo_key] = mode
    _save_settings()
    executor.submit(bg_refresh_single_repo, repo_key, True)


def _ctx_refresh_repo(sender, app_data, user_data):
    """Context-menu action: force-refresh a single repo."""
    repo_key = user_data
    if dpg.does_item_exist("repo_context_menu"):
        dpg.delete_item("repo_context_menu")
    rs = app.repos.get(repo_key)
    if rs and rs.header_tag and dpg.does_item_exist(rs.header_tag):
        old_label = dpg.get_item_label(rs.header_tag)
        if not old_label.endswith(" ..."):
            dpg.configure_item(rs.header_tag, label=old_label + "  ...")
    # Allow this repo to expand on the next rebuild even if globally paused
    app.expand_on_next_build.add(repo_key)
    executor.submit(bg_refresh_single_repo, repo_key, True)


def cb_pause(sender, app_data):
    app.paused = not app.paused
    if app.paused:
        dpg.configure_item("pause_btn", label="Paused")
        dpg.bind_item_theme("pause_btn", "pause_active_theme")
    else:
        dpg.configure_item("pause_btn", label="Pause")
        dpg.bind_item_theme("pause_btn", 0)
        trigger_poll()


def cb_poll_changed(sender, app_data):
    try:
        val = int(dpg.get_value(sender))
        if val < 5:
            val = 5
        app.poll_interval = val
        _save_settings()
    except (ValueError, TypeError):
        pass


def cb_auto_generate(sender, app_data):
    app.auto_generate = dpg.get_value(sender)
    _save_settings()


def cb_always_on_top(sender, app_data):
    app.always_on_top = dpg.get_value(sender)
    _set_topmost(app.always_on_top)
    _save_settings()


def cb_actions_popup(sender, app_data):
    app.actions_popup_enabled = dpg.get_value(sender)
    _save_settings()


def cb_chime_on_completion(sender, app_data):
    app.chime_on_completion = dpg.get_value(sender)
    _save_settings()


def cb_test_chime(sender, app_data, user_data):
    chime.play(success=bool(user_data))


def cb_show_non_git(sender, app_data):
    app.show_non_git_folders = dpg.get_value(sender)
    _save_settings()
    trigger_poll()


def cb_recent_only(sender, app_data):
    """Toggle the recency display filter. Re-renders from the last poll payload --
    applying/removing the filter costs no git work."""
    app.recent_only = bool(dpg.get_value(sender))
    # Keep the toolbar + Settings checkboxes in sync (whichever was toggled).
    for tag in ("recent_only_cb", "settings_recent_only_cb"):
        if tag != sender and dpg.does_item_exist(tag):
            dpg.set_value(tag, app.recent_only)
    _save_settings()
    if app.last_results or app.last_non_git:
        rebuild_repos_ui(app.last_results, app.last_non_git, preserve_open=True)
    else:
        trigger_poll()


def cb_sort_by_date(sender, app_data):
    app.sort_by_date = bool(dpg.get_value(sender))
    _save_settings()
    if app.last_results or app.last_non_git:
        rebuild_repos_ui(app.last_results, app.last_non_git, preserve_open=True)
    else:
        trigger_poll()


def cb_recent_days(sender, app_data):
    try:
        val = int(dpg.get_value(sender))
        if val < 1:
            val = 1
        app.recent_days = val
        _save_settings()
        trigger_poll()  # window changed -> re-tier and re-filter
    except (ValueError, TypeError):
        pass


def cb_idle_interval(sender, app_data):
    try:
        val = int(dpg.get_value(sender))
        if val < 60:
            val = 60
        app.idle_poll_interval = val
        _save_settings()
        trigger_poll()
    except (ValueError, TypeError):
        pass


def cb_open_settings(sender, app_data):
    """Open the settings popup window."""
    win_tag = "settings_window"
    if dpg.does_item_exist(win_tag):
        dpg.focus_item(win_tag)
        return
    with dpg.window(
        label="Settings",
        tag=win_tag,
        width=340, height=400,
        no_collapse=True,
        on_close=lambda s, a, u: (
            dpg.delete_item(s) if dpg.does_item_exist(s) else None
        ),
    ):
        dpg.add_text("Polling", color=COL_ACCENT)
        with dpg.group(horizontal=True):
            dpg.add_text("Poll interval:", color=COL_DIM)
            dpg.add_input_int(default_value=app.poll_interval, width=80,
                              min_value=5, min_clamped=True, max_value=600, max_clamped=True,
                              callback=cb_poll_changed, step=0)
            dpg.add_text("s", color=COL_DIM)
        dpg.add_spacer(height=6)
        dpg.add_text("Behavior", color=COL_ACCENT)
        dpg.add_checkbox(label="Auto-generate commit messages",
                         default_value=app.auto_generate,
                         callback=cb_auto_generate)
        dpg.add_checkbox(label="Always on top",
                         default_value=app.always_on_top,
                         callback=cb_always_on_top)
        dpg.add_checkbox(label="Actions popup after push",
                         default_value=app.actions_popup_enabled,
                         callback=cb_actions_popup)
        dpg.add_checkbox(label="Chime when workflow completes",
                         default_value=app.chime_on_completion,
                         callback=cb_chime_on_completion)
        with dpg.group(horizontal=True):
            dpg.add_spacer(width=20)
            dpg.add_button(label="Test OK",
                           callback=cb_test_chime, user_data=True)
            dpg.add_button(label="Test Fail",
                           callback=cb_test_chime, user_data=False)
        if sys.platform == "win32":
            dpg.add_checkbox(label="Run at startup",
                             default_value=_is_startup_enabled(),
                             callback=cb_start_with_windows)
        dpg.add_spacer(height=6)
        dpg.add_text("Provider", color=COL_ACCENT)
        with dpg.group(horizontal=True):
            dpg.add_text("Ollama URL:", color=COL_DIM)
            dpg.add_input_text(tag="settings_ollama_url",
                               default_value=app.ollama_url, width=-1,
                               callback=cb_ollama_url_changed, on_enter=True,
                               hint="http://localhost:11434")
        dpg.add_spacer(height=6)
        dpg.add_text("Display", color=COL_ACCENT)
        dpg.add_checkbox(label="Show non-git folders",
                         default_value=app.show_non_git_folders,
                         callback=cb_show_non_git)
        dpg.add_checkbox(label="Recent repos only", tag="settings_recent_only_cb",
                         default_value=app.recent_only,
                         callback=cb_recent_only)
        with dpg.group(horizontal=True):
            dpg.add_text("Recent window:", color=COL_DIM)
            dpg.add_input_int(default_value=app.recent_days, width=80,
                              min_value=1, min_clamped=True,
                              max_value=3650, max_clamped=True,
                              callback=cb_recent_days, step=0)
            dpg.add_text("days", color=COL_DIM)
        with dpg.group(horizontal=True):
            dpg.add_text("Idle repo poll:", color=COL_DIM)
            dpg.add_input_int(default_value=app.idle_poll_interval, width=80,
                              min_value=60, min_clamped=True,
                              max_value=86400, max_clamped=True,
                              callback=cb_idle_interval, step=0)
            dpg.add_text("s", color=COL_DIM)
        dpg.add_spacer(height=10)
        def _save_and_close():
            if dpg.does_item_exist("settings_ollama_url"):
                val = dpg.get_value("settings_ollama_url").strip()
                if val:
                    app.ollama_url = val
            _save_settings()
            if dpg.does_item_exist("settings_window"):
                dpg.delete_item("settings_window")

        save_btn = dpg.add_button(label="Save & Close", callback=_save_and_close)
        dpg.bind_item_theme(save_btn, green_btn_theme)


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
    # Mirror the single-repo Refresh path (_ctx_refresh_repo): the status
    # refresh below rebuilds the repo list, which would otherwise re-collapse
    # this header while globally paused. Flag it to stay expanded across that
    # one rebuild (the flag is consumed in the build at expand_on_next_build).
    app.expand_on_next_build.add(repo_name)
    # Refresh repo status first, then generate (handled in queue processor)
    executor.submit(bg_refresh_then_generate, repo_name)


def cb_open_repo_url(sender, app_data, user_data):
    if user_data:
        webbrowser.open(user_data)


def cb_create_remote(sender, app_data, user_data):
    """Detect GitHub accounts then show create-remote popup."""
    repo_key = user_data
    rs = app.repos.get(repo_key)
    if not rs:
        return
    # Capture click position so the popup opens nearby.
    click_pos = dpg.get_mouse_pos()
    dpg.set_value(rs.status_tag, "Detecting GitHub accounts...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_detect_gh_accounts, repo_key, click_pos)


def _show_create_remote_popup(repo_key, accounts, active_account,
                              click_pos=(0, 0)):
    """Show popup dialog for creating a GitHub remote."""
    rs = app.repos.get(repo_key)
    if not rs:
        return

    win_tag = dpg.generate_uuid()
    combo_tag = dpg.generate_uuid()
    radio_tag = dpg.generate_uuid()

    folder_name = rs.path.name
    default_acct = (active_account if active_account in accounts
                    else accounts[0] if accounts else "")

    # Position the popup near where the user clicked.
    pop_w, pop_h = 400, 220
    px = max(0, int(click_pos[0]) - pop_w // 2)
    py = max(0, int(click_pos[1]))

    with dpg.window(
        label=f"Create GitHub Repo \u2014 {folder_name}",
        tag=win_tag,
        width=pop_w, height=pop_h,
        pos=(px, py),
        no_collapse=True,
        on_close=lambda s, a, u: (
            dpg.delete_item(s) if dpg.does_item_exist(s) else None
        ),
    ):
        with dpg.group(horizontal=True):
            dpg.add_text("Account:", color=COL_ACCENT)
            add_btn = dpg.add_button(
                label="+ Add Account",
                callback=_cb_add_gh_account,
                user_data=win_tag,
            )
            dpg.bind_item_theme(add_btn, link_btn_theme)
        if accounts:
            dpg.add_combo(
                accounts, tag=combo_tag,
                default_value=default_acct, width=-1,
            )
        else:
            dpg.add_text("No accounts found - add one above.",
                         color=COL_DIM)
            dpg.add_combo([], tag=combo_tag, width=-1)
        dpg.add_spacer(height=6)
        dpg.add_text("Visibility:", color=COL_ACCENT)
        dpg.add_radio_button(
            ["Private", "Public"], tag=radio_tag,
            default_value="Private", horizontal=True,
        )
        dpg.add_spacer(height=10)
        with dpg.group(horizontal=True):
            create_btn = dpg.add_button(
                label="Create",
                callback=_cb_confirm_create_remote,
                user_data=(repo_key, win_tag, combo_tag, radio_tag),
            )
            dpg.bind_item_theme(create_btn, green_btn_theme)
            if not accounts:
                dpg.configure_item(create_btn, enabled=False)
            dpg.add_button(
                label="Cancel",
                user_data=win_tag,
                callback=lambda s, a, u: (
                    dpg.delete_item(u) if dpg.does_item_exist(u) else None
                ),
            )


def _cb_add_gh_account(sender, app_data, user_data):
    """Open a terminal to run gh auth login, then close the popup."""
    win_tag = user_data
    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/c", "start", "cmd", "/k", "gh auth login"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    elif sys.platform == "darwin":
        subprocess.Popen(
            ["open", "-a", "Terminal",
             "bash", "-c", "gh auth login; exec bash"],
        )
    else:
        for term in ("gnome-terminal", "konsole", "xterm"):
            if shutil.which(term):
                subprocess.Popen([term, "--", "bash", "-c",
                                  "gh auth login; exec bash"])
                break


def _cb_confirm_create_remote(sender, app_data, user_data):
    """User confirmed create-remote from the popup."""
    repo_key, win_tag, combo_tag, radio_tag = user_data

    account = dpg.get_value(combo_tag)
    visibility_label = dpg.get_value(radio_tag)
    visibility = "private" if visibility_label == "Private" else "public"

    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)

    rs = app.repos.get(repo_key)
    if not rs:
        return
    dpg.set_value(rs.status_tag, f"Creating {visibility} repo on {account}...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_create_remote, repo_key, account, visibility)


def _show_upstream_prompt(repo_name, branch):
    """Show a popup asking the user to push with --set-upstream."""
    win_tag = dpg.generate_uuid()
    pop_w, pop_h = 440, 130
    click_pos = dpg.get_mouse_pos()
    px = max(0, int(click_pos[0]) - pop_w // 2)
    py = max(0, int(click_pos[1]))

    with dpg.window(
        label="Set Upstream Branch",
        tag=win_tag,
        width=pop_w, height=pop_h,
        pos=(px, py),
        no_collapse=True,
        on_close=lambda s, a, u: (
            dpg.delete_item(s) if dpg.does_item_exist(s) else None
        ),
    ):
        dpg.add_text(f"Branch '{branch}' has no remote tracking branch.")
        dpg.add_text(f"Run: git push --set-upstream origin {branch}",
                     color=COL_DIM)
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            push_btn = dpg.add_button(
                label="Push & Set Upstream",
                callback=_cb_confirm_upstream,
                user_data=(repo_name, branch, win_tag),
            )
            dpg.bind_item_theme(push_btn, green_btn_theme)
            dpg.add_button(
                label="Cancel",
                user_data=win_tag,
                callback=lambda s, a, u: (
                    dpg.delete_item(u) if dpg.does_item_exist(u) else None
                ),
            )


def _cb_confirm_upstream(sender, app_data, user_data):
    """User confirmed push --set-upstream."""
    repo_name, branch, win_tag = user_data
    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)
    rs = app.repos.get(repo_name)
    if not rs:
        return
    dpg.set_value(rs.status_tag, f"Pushing to origin/{branch}...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_push_set_upstream, repo_name, branch)


def _show_secret_push_prompt(repo_name, branch=""):
    """Push blocked by GitLab secret push protection -- offer a one-time skip."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    win_tag = dpg.generate_uuid()
    pop_w, pop_h = 520, 170
    click_pos = dpg.get_mouse_pos()
    px = max(0, int(click_pos[0]) - pop_w // 2)
    py = max(0, int(click_pos[1]))

    cmd = f"git push -o {SECRET_PUSH_SKIP_OPTION}"
    if branch:
        cmd += f" --set-upstream origin {branch}"

    with dpg.window(
        label=f"Secret Push Protection -- {rs.name}",
        tag=win_tag,
        width=pop_w, height=pop_h,
        pos=(px, py),
        no_collapse=True,
        on_close=lambda s, a, u: (
            dpg.delete_item(s) if dpg.does_item_exist(s) else None
        ),
    ):
        dpg.add_text("GitLab blocked this push: secrets detected in the commit.",
                     color=COL_RED)
        dpg.add_text("Remove the secrets and amend, or skip protection for "
                     "this one push.")
        dpg.add_text(f"Run: {cmd}", color=COL_DIM)
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            push_btn = dpg.add_button(
                label="Push Anyway (skip protection)",
                callback=_cb_confirm_secret_push,
                user_data=(repo_name, branch, win_tag),
            )
            dpg.bind_item_theme(push_btn, pull_btn_theme)
            dpg.add_button(
                label="Cancel",
                user_data=win_tag,
                callback=lambda s, a, u: (
                    dpg.delete_item(u) if dpg.does_item_exist(u) else None
                ),
            )


def _cb_confirm_secret_push(sender, app_data, user_data):
    """User confirmed pushing past secret push protection."""
    repo_name, branch, win_tag = user_data
    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)
    rs = app.repos.get(repo_name)
    if not rs:
        return
    dpg.set_value(rs.status_tag, "Pushing (skipping secret protection)...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_push_override, repo_name, branch)


def cb_open_terminal(sender, app_data, user_data):
    if not user_data:
        return
    path = str(user_data)
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Terminal", path])
    elif sys.platform == "win32":
        subprocess.Popen(["cmd.exe", "/k"], creationflags=subprocess.CREATE_NEW_CONSOLE, cwd=path)
    else:
        subprocess.Popen(["x-terminal-emulator", "--working-directory", path])


def cb_open_folder(sender, app_data, user_data):
    """Open a folder in Finder (macOS) or Explorer (Windows)."""
    if not user_data:
        return
    path = str(user_data)
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", path], creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        subprocess.Popen(["xdg-open", path])


def cb_open_file(sender, app_data, user_data):
    """Open a file with the system default application."""
    repo_path, filepath = user_data
    full_path = str(Path(repo_path) / filepath)
    if sys.platform == "darwin":
        subprocess.Popen(["open", full_path])
    elif sys.platform == "win32":
        os.startfile(full_path)
    else:
        subprocess.Popen(["xdg-open", full_path])


def cb_view_diff(sender, app_data, user_data):
    """Launch a separate diff viewer window for a modified file."""
    repo_path, filepath = user_data
    executor.submit(bg_launch_diff_viewer, repo_path, filepath)


def bg_launch_diff_viewer(repo_path, filepath):
    """Get the diff and launch a separate viewer window as a subprocess."""
    rc, stdout, _ = run_git(["diff", "HEAD", "--", filepath], cwd=repo_path)
    if rc != 0 or not stdout.strip():
        rc2, stdout2, _ = run_git(["diff", "--cached", "--", filepath], cwd=repo_path)
        if rc2 == 0 and stdout2.strip():
            stdout = stdout2
        elif not stdout.strip():
            stdout = (describe_empty_diff(repo_path, only_path=filepath)
                      or "(no diff available)")
    data = {"filepath": filepath, "diff": stdout}
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
        dir=tempfile.gettempdir(), encoding="utf-8",
    )
    json.dump(data, tmp)
    tmp.close()
    viewer = str(Path(__file__).resolve().parent / "diff_viewer.py")
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        pw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(pw):
            exe = pw
    subprocess.Popen([exe, viewer, tmp.name])


def cb_preview_pull(sender, app_data, user_data):
    """Fetch and preview incoming changes before pulling."""
    repo_key = user_data
    rs = app.repos.get(repo_key)
    if not rs:
        return
    dpg.set_value(rs.status_tag, "Fetching preview...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_preview_pull, repo_key)


def cb_confirm_pull(sender, app_data, user_data):
    """User confirmed pull from the preview window."""
    repo_key, win_tag = user_data
    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)
    rs = app.repos.get(repo_key)
    if not rs:
        return
    dpg.set_value(rs.status_tag, "Pulling...")
    dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_pull, repo_key)


def cb_close_preview(sender, app_data, user_data):
    """Close preview window without pulling."""
    if dpg.does_item_exist(user_data):
        dpg.delete_item(user_data)


def cb_clean_preview(sender, app_data, user_data):
    """Run git clean -nd to preview what would be removed."""
    repo_key = user_data
    rs = app.repos.get(repo_key)
    if not rs:
        return
    if rs.status_tag and dpg.does_item_exist(rs.status_tag):
        dpg.set_value(rs.status_tag, "Checking for files to clean...")
        dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_clean_preview, repo_key)


def _run_git_no_stdin(args, cwd):
    """Run a git command with stdin closed so interactive prompts get EOF."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    return result.returncode, result.stdout, result.stderr


def bg_clean_preview(repo_key):
    """Run git clean -nd. Posts result to ui_queue."""
    try:
        rc, stdout, stderr = _run_git_no_stdin(["clean", "-nd"], cwd=repo_key)
        if rc != 0:
            ui_queue.put(("clean_preview_result", repo_key, False,
                          stderr.strip() or "git clean failed"))
        else:
            ui_queue.put(("clean_preview_result", repo_key, True, stdout))
    except Exception as exc:
        ui_queue.put(("clean_preview_result", repo_key, False, str(exc)))


def cb_confirm_clean(sender, app_data, user_data):
    """User confirmed clean from the preview window."""
    repo_key, win_tag = user_data
    if dpg.does_item_exist(win_tag):
        dpg.delete_item(win_tag)
    rs = app.repos.get(repo_key)
    if not rs:
        return
    if rs.status_tag and dpg.does_item_exist(rs.status_tag):
        dpg.set_value(rs.status_tag, "Cleaning...")
        dpg.configure_item(rs.status_tag, color=COL_YELLOW)
    executor.submit(bg_clean_confirm, repo_key)


def cb_close_clean_preview(sender, app_data, user_data):
    """Close clean preview window without cleaning."""
    if dpg.does_item_exist(user_data):
        dpg.delete_item(user_data)


def _shell_delete(path):
    """Delete a file or directory via the Windows Shell API (same as Explorer).

    Falls back to shutil/os on non-Windows.
    """
    if sys.platform == "win32":
        import ctypes.wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.wintypes.HWND),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        FO_DELETE = 0x0003
        FOF_SILENT = 0x0004
        FOF_NOCONFIRMATION = 0x0010
        FOF_NOERRORUI = 0x0400

        op = SHFILEOPSTRUCTW()
        op.hwnd = 0
        op.wFunc = FO_DELETE
        op.pFrom = str(path) + "\0"
        op.pTo = None
        op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if result != 0:
            raise OSError(f"SHFileOperation error code 0x{result:04X}")
    else:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def bg_clean_confirm(repo_key):
    """Delete untracked files/dirs via Windows Shell API (same mechanism as Explorer)."""
    try:
        rc, stdout, _ = _run_git_no_stdin(["clean", "-nd"], cwd=repo_key)
        if rc != 0:
            ui_queue.put(("clean_result", repo_key, False, [], ["git clean -nd failed"]))
            return
        removed = []
        errors = []
        repo_path = Path(repo_key)
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("Would remove "):
                continue
            rel = line[len("Would remove "):]
            target = repo_path / rel
            try:
                _shell_delete(target)
                removed.append(f"Removing {rel}")
            except Exception as exc:
                errors.append(f"Failed to remove {rel}: {exc}")
        ok = not errors
        ui_queue.put(("clean_result", repo_key, ok, removed, errors))
    except Exception as exc:
        ui_queue.put(("clean_result", repo_key, False, [], [str(exc)]))


def cb_gitignore(sender, app_data, user_data):
    """Add a file or folder to the repo's .gitignore and refresh."""
    repo_key, filepath = user_data
    repo_path = Path(repo_key)
    gitignore = repo_path / ".gitignore"
    entry = filepath.rstrip("/")
    # Check if already present
    existing = ""
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8", errors="replace")
        if entry in {line.strip() for line in existing.splitlines()}:
            trigger_poll()
            return
    # Append entry (ensure trailing newline before our addition)
    with open(gitignore, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(entry + "\n")
    trigger_poll()


def cb_remove_folder(sender, app_data, user_data):
    """Remove a watched folder."""
    folder = Path(user_data)
    if folder in app.watched_folders:
        app.watched_folders.remove(folder)
        _save_settings()
        _rebuild_folders_ui()
        trigger_poll()


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
    if rs.remote_url:
        dpg.set_value(rs.status_tag, "Committing & pushing...")
    else:
        dpg.set_value(rs.status_tag, "Committing...")
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
    app.provider = "ollama"
    dpg.set_value("model_input", _DEFAULT_MODEL)
    dpg.set_value("provider_combo", "ollama")


def cb_provider_changed(sender, app_data):
    app.provider = dpg.get_value(sender)


def cb_ollama_url_changed(sender, app_data):
    val = dpg.get_value(sender).strip()
    if val:
        app.ollama_url = val
        _save_settings()


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
        tray_icon.title = "AI Commit Monitor - changes detected"
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

def trigger_poll(force=False):
    app.last_poll = time.time()
    for repo_key, rs in app.repos.items():
        if rs.header_tag and dpg.does_item_exist(rs.header_tag):
            override = app.repo_overrides.get(repo_key, "")
            skip = (override == "pause"
                    or (not force and app.paused and override != "active"))
            if skip:
                continue
            old_label = dpg.get_item_label(rs.header_tag)
            if not old_label.endswith(" ..."):
                dpg.configure_item(rs.header_tag, label=old_label + "  ...")
    executor.submit(bg_poll_repos, force)


def _rebuild_folders_ui():
    """Rebuild the watched-folders list in the UI."""
    if not dpg.does_item_exist("folders_container"):
        return
    dpg.delete_item("folders_container", children_only=True)
    if not app.watched_folders:
        dpg.add_text("No folders - click Add Folder", color=COL_DIM,
                      parent="folders_container")
        return
    for folder in app.watched_folders:
        with dpg.group(horizontal=True, parent="folders_container"):
            rm = dpg.add_button(label="x", callback=cb_remove_folder,
                                user_data=str(folder))
            dpg.bind_item_theme(rm, remove_btn_theme)
            link = dpg.add_button(label=str(folder),
                                  callback=cb_open_folder,
                                  user_data=str(folder))
            dpg.bind_item_theme(link, link_btn_theme)


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


def _repo_base_label(rs):
    """Return the base header label (without the date portion)."""
    change_count = len(rs.entries)
    name_part = rs.name
    # Flag folder name mismatch with a marker
    if rs.folder_name != rs.name:
        name_part = f"* {name_part}"
    if change_count:
        label = f"{name_part} ({change_count} change{'s' if change_count != 1 else ''})"
    else:
        label = name_part
    if rs.behind > 0:
        label += f"  !! {rs.behind} BEHIND"
    elif rs.ahead > 0:
        label += f"  !! {rs.ahead} NOT PUSHED"
    if rs.branch_status:
        label += f"  ({rs.branch_status})"
    return label


def build_repo_section(rs, parent, label_width=0, preserve_open=False,
                       prior_open=None):
    """Build the UI section for a single repo inside *parent*.

    When *preserve_open* is True (partial rebuilds such as a single-repo
    refresh), the header's open/collapse state is taken from *prior_open*
    (keyed by repo path) so repos are not auto-collapsed. On full refreshes
    (poll loop / Refresh-all) preserve_open is False and the activity-based
    default applies.
    """
    change_count = len(rs.entries)
    label = _repo_base_label(rs)
    show_account = rs.github_account and rs.github_account != app.active_gh_account
    is_public = rs.visibility == "PUBLIC"
    vis_label = "** PUBLIC **" if is_public else (rs.visibility.lower() if rs.visibility else ("LOCAL" if not rs.remote_url else ""))

    right_parts = []
    if rs.last_commit_date:
        right_parts.append(f"[{rs.last_commit_date}]")
    if vis_label:
        right_parts.append(vis_label)
    if rs.branch:
        right_parts.append(f"[{rs.branch}]")
    if show_account:
        right_parts.append(f"[{rs.github_account}]")
    if right_parts:
        pad = max(0, label_width - len(label))
        label += " " * pad + "  " + " ".join(right_parts)

    repo_key = str(rs.path)
    override = app.repo_overrides.get(repo_key, "")
    has_activity = change_count > 0 or rs.behind > 0 or rs.ahead > 0
    force_expand = repo_key in app.expand_on_next_build
    if force_expand:
        app.expand_on_next_build.discard(repo_key)
    force_collapse = repo_key in app.collapse_on_next_build
    if force_collapse:
        app.collapse_on_next_build.discard(repo_key)
    has_prior = bool(preserve_open) and prior_open is not None and repo_key in prior_open
    should_open = compute_header_open(
        override=override,
        paused=app.paused,
        has_activity=has_activity,
        force_expand=force_expand,
        force_collapse=force_collapse,
        preserve_open=bool(preserve_open),
        has_prior=has_prior,
        prior_open=prior_open.get(repo_key) if has_prior else False,
    )

    rs.header_tag = dpg.add_collapsing_header(
        label=label,
        parent=parent,
        default_open=should_open,
    )
    with dpg.item_handler_registry() as rclick_handler:
        dpg.add_item_clicked_handler(button=dpg.mvMouseButton_Right,
                                     callback=cb_repo_right_click,
                                     user_data=repo_key)
    dpg.bind_item_handler_registry(rs.header_tag, rclick_handler)

    if override == "pause":
        dpg.bind_item_theme(rs.header_tag, "force_pause_header_theme")
    elif override == "active":
        dpg.bind_item_theme(rs.header_tag, "force_active_header_theme")
    elif is_public:
        dpg.bind_item_theme(rs.header_tag, "public_header_theme")

    # Sync warning banner -- prominent when behind remote
    if rs.behind > 0 or rs.ahead > 0:
        repo_key = str(rs.path)
        parts = []
        if rs.behind > 0:
            parts.append(f"{rs.behind} commit{'s' if rs.behind != 1 else ''} BEHIND remote")
        if rs.ahead > 0:
            parts.append(f"{rs.ahead} commit{'s' if rs.ahead != 1 else ''} ahead")
        sync_text = " / ".join(parts)

        if rs.behind > 0:
            with dpg.group(horizontal=True, parent=rs.header_tag):
                dpg.add_text(f"  !! {sync_text} - PULL BEFORE EDITING !!", color=COL_RED)
                pull_btn = dpg.add_button(label="Preview Pull", callback=cb_preview_pull, user_data=repo_key)
                dpg.bind_item_theme(pull_btn, pull_btn_theme)
        else:
            dpg.add_text(f"  !! {sync_text} - PUSH REQUIRED !!", color=COL_RED, parent=rs.header_tag)

    # Folder name mismatch warning
    if rs.folder_name != rs.name:
        dpg.add_text(
            f"  ** Folder mismatch: folder is \"{rs.folder_name}\" but repo is \"{rs.name}\" **",
            color=COL_YELLOW, parent=rs.header_tag)

    # Links row: Terminal, Open Folder, GitHub, More
    with dpg.group(horizontal=True, parent=rs.header_tag):
        term_btn = dpg.add_button(
            label="Terminal",
            callback=cb_open_terminal, user_data=str(rs.path))
        dpg.bind_item_theme(term_btn, link_btn_theme)
        folder_btn = dpg.add_button(
            label="Folder",
            callback=cb_open_folder, user_data=str(rs.path))
        dpg.bind_item_theme(folder_btn, link_btn_theme)
        clean_btn = dpg.add_button(
            label="Clean",
            callback=cb_clean_preview, user_data=str(rs.path))
        dpg.bind_item_theme(clean_btn, link_btn_theme)
        if rs.remote_url:
            btn = dpg.add_button(label="GitHub", callback=cb_open_repo_url, user_data=rs.remote_url)
            dpg.bind_item_theme(btn, link_btn_theme)
        else:
            btn = dpg.add_button(label="Create-Remote", callback=cb_create_remote, user_data=str(rs.path))
            dpg.bind_item_theme(btn, link_btn_theme)
        more_btn = dpg.add_button(label="More", callback=cb_more, user_data=str(rs.path))
        dpg.bind_item_theme(more_btn, link_btn_theme)

    # Expandable MORE panel (populated lazily on click)
    rs.more_group_tag = dpg.add_group(parent=rs.header_tag, show=False)

    # Latest commit -- show only the subject (first line) inline. The full
    # message (subject + body) lives in the MORE panel; a trailing "…" hints
    # that there's more to see when the message has a body. The date is omitted
    # here since it already appears in the header label (see _repo_base_label).
    if rs.last_commit_msg:
        subject, _, body = rs.last_commit_msg.partition("\n")
        subject = subject.strip()
        more_hint = " ..." if body.strip() else ""
        dpg.add_text(f"  latest: {subject}{more_hint}", color=COL_DIM,
                     parent=rs.header_tag, wrap=0)

    rs.files_group_tag = dpg.add_group(parent=rs.header_tag)
    repo_key = str(rs.path)
    for code, filepath in rs.entries:
        lbl = STATUS_LABELS.get(code, code)
        color = COL_GREEN if code in ("A", "AM", "??") else COL_YELLOW if code in ("M", "MM") else COL_RED if code == "D" else COL_DIM
        with dpg.group(horizontal=True, parent=rs.files_group_tag):
            dpg.add_text(f"  {lbl:>10}", color=color)
            file_btn = dpg.add_button(
                label=f"  {filepath}",
                callback=cb_open_file,
                user_data=(str(rs.path), filepath),
            )
            dpg.bind_item_theme(file_btn, link_btn_theme)
            if code in ("M", "MM", "AM"):
                diff_btn = dpg.add_button(
                    label="View Diff",
                    callback=cb_view_diff,
                    user_data=(str(rs.path), filepath),
                )
                dpg.bind_item_theme(diff_btn, link_btn_theme)
            if code == "??":
                btn = dpg.add_button(
                    label="gitignore",
                    callback=cb_gitignore,
                    user_data=(str(rs.path), filepath),
                )
                dpg.bind_item_theme(btn, link_btn_theme)

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

        # Status line (wrap=0 -> wrap at panel width; error text can be several
        # lines long, e.g. the EOL-only explanation from describe_empty_diff)
        rs.status_tag = dpg.add_text("", parent=rs.header_tag, wrap=0)
        update_repo_status(rs)

        # Buttons row
        with dpg.group(horizontal=True, parent=rs.header_tag):
            repo_key = str(rs.path)
            rs.gen_btn_tag = dpg.add_button(label="Generate", callback=cb_generate, user_data=repo_key)
            if rs.remote_url:
                rs.accept_btn_tag = dpg.add_button(label="Commit & Push", callback=cb_accept, user_data=repo_key)
                dpg.bind_item_theme(rs.accept_btn_tag, green_btn_theme)
            else:
                rs.accept_btn_tag = dpg.add_button(label="Commit", callback=cb_accept, user_data=repo_key)
                dpg.bind_item_theme(rs.accept_btn_tag, orange_btn_theme)

        dpg.add_spacer(height=4, parent=rs.header_tag)
    else:
        if rs.gen_status == GenStatus.ERROR and rs.error_message:
            rs.status_tag = dpg.add_text(
                f"Error: {rs.error_message}",
                color=COL_RED, parent=rs.header_tag, wrap=0)
        else:
            rs.status_tag = dpg.add_text("Clean", color=COL_DIM, parent=rs.header_tag)
        rs.input_tag = 0


def build_non_git_section(ngf, parent, preserve_open=False, prior_open=None):
    """Build a minimal UI section for a non-git folder with an Init button.

    Like repos, a user-expanded folder keeps its open state across partial
    rebuilds when *preserve_open* is True (state taken from *prior_open*,
    keyed by folder path).
    """
    ngf_key = str(ngf.path)
    default_open = False
    if preserve_open and prior_open is not None and ngf_key in prior_open:
        default_open = prior_open[ngf_key]
    ngf.header_tag = dpg.add_collapsing_header(
        label=f"{ngf.name}  (not a git repo)",
        parent=parent,
        default_open=default_open,
    )
    with dpg.group(horizontal=True, parent=ngf.header_tag):
        term_btn = dpg.add_button(
            label="Terminal",
            callback=cb_open_terminal, user_data=str(ngf.path))
        dpg.bind_item_theme(term_btn, link_btn_theme)
        folder_btn = dpg.add_button(
            label="Folder",
            callback=cb_open_folder, user_data=str(ngf.path))
        dpg.bind_item_theme(folder_btn, link_btn_theme)
        init_btn = dpg.add_button(
            label="Init",
            callback=cb_git_init, user_data=str(ngf.path))
        dpg.bind_item_theme(init_btn, green_btn_theme)
    ngf.status_tag = dpg.add_text("", parent=ngf.header_tag)


def cb_git_init(sender, app_data, user_data):
    """Initialize a git repo in the given folder."""
    executor.submit(bg_git_init, user_data)


def bg_git_init(folder_path):
    """Run git init in a folder. Posts result to ui_queue."""
    try:
        rc, stdout, stderr = run_git(["init", "-b", "main"], cwd=folder_path)
        if rc == 0:
            ui_queue.put(("git_init_result", folder_path, True, stdout.strip()))
        else:
            ui_queue.put(("git_init_result", folder_path, False, stderr.strip()))
    except Exception as exc:
        ui_queue.put(("git_init_result", folder_path, False, str(exc)))


# ---------------------------------------------------------------------------
# MORE panel
# ---------------------------------------------------------------------------

def cb_more(sender, app_data, user_data):
    """Toggle the MORE panel. On first open, fetch data lazily."""
    repo_key = user_data
    rs = app.repos.get(repo_key)
    if not rs:
        return
    if rs.more_group_tag and dpg.does_item_exist(rs.more_group_tag):
        is_shown = dpg.is_item_shown(rs.more_group_tag)
        if is_shown:
            dpg.configure_item(rs.more_group_tag, show=False)
            return
        dpg.configure_item(rs.more_group_tag, show=True)
    dpg.delete_item(rs.more_group_tag, children_only=True)
    dpg.add_text("  Loading...", color=COL_DIM, parent=rs.more_group_tag)
    executor.submit(bg_fetch_more_data, repo_key)


def bg_fetch_more_data(repo_key):
    """Fetch all data needed for the MORE panel. Posts result to ui_queue."""
    rs = app.repos.get(repo_key)
    if not rs:
        return
    cwd = str(rs.path)

    # A. Gitignored files (--directory collapses ignored dirs into single entries)
    rc, stdout, _ = run_git(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
        cwd=cwd)
    ignored_files = stdout.strip().splitlines() if rc == 0 and stdout.strip() else []

    # B. Branches (local + remote-only). branch_targets maps the dropdown label
    # to the git args used to switch to it. Remote-only entries get an explicit
    # tracking checkout so multi-remote repos still resolve unambiguously.
    local_branches = []
    current_branch = ""
    rc, stdout, _ = run_git(["branch", "--list"], cwd=cwd)
    if rc == 0:
        for line in stdout.splitlines():
            line_s = line.strip()
            if line_s.startswith("* "):
                current_branch = line_s[2:].strip()
                local_branches.append(current_branch)
            elif line_s:
                local_branches.append(line_s)

    # Annotate local branches with "(local only)" / "(stale)" so deleted
    # remote branches and never-pushed branches are visible at a glance.
    # `deletable` maps "name  (status)" labels -> branch name for the
    # Delete-branch dropdown; only local-only / stale entries qualify.
    classifications = get_branch_classification(cwd)
    branch_options = []
    branch_targets = {}
    deletable = {}
    for b in local_branches:
        status = classifications.get(b, "")
        label = f"{b}  ({status})" if status else b
        branch_options.append(label)
        branch_targets[label] = ["checkout", b]
        if status in ("local only", "stale") and b != current_branch:
            deletable[label] = b

    rc, stdout, _ = run_git(
        ["branch", "-r", "--format=%(refname:short)"], cwd=cwd)
    if rc == 0:
        seen_short = set(local_branches)
        for line in stdout.splitlines():
            full_ref = line.strip()
            # Skip blanks, HEAD pointers, and symbolic refs like origin/HEAD
            if not full_ref or "->" in full_ref:
                continue
            parts = full_ref.split("/", 1)
            if len(parts) != 2:
                continue
            remote_name, short = parts
            if not short or short == "HEAD" or short in seen_short:
                continue
            seen_short.add(short)
            label = f"{short}  (remote: {remote_name})"
            branch_options.append(label)
            branch_targets[label] = [
                "checkout", "-b", short, "--track", full_ref,
            ]

    # C. Dispatchable workflows that apply to the current branch:
    # active + file exists on this branch + has a workflow_dispatch trigger.
    workflows = []
    if rs.remote_url and current_branch:
        owner, repo_name = parse_owner_repo(rs.remote_url)
        token = get_gh_token()
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["gh", "workflow", "list", "--json", "name,id,state,path"],
                cwd=cwd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, **kwargs,
            )
            if result.returncode == 0 and result.stdout.strip() and owner and repo_name and token:
                wf_list = json.loads(result.stdout)
                for w in wf_list:
                    if w.get("state") != "active":
                        continue
                    path = w.get("path", "")
                    if not path:
                        continue
                    yaml_text = fetch_workflow_yaml(
                        owner, repo_name, path, current_branch, token,
                    )
                    if yaml_text is None:
                        continue
                    if not has_workflow_dispatch(yaml_text):
                        continue
                    workflows.append({"name": w["name"], "id": w["id"]})
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    ui_queue.put(("more_data_result", repo_key, {
        "ignored_files": ignored_files,
        "branches": branch_options,
        "branch_targets": branch_targets,
        "local_branch_names": list(local_branches),
        "deletable": deletable,
        "current_branch": current_branch,
        "workflows": workflows,
    }))


def _build_more_panel(rs, repo_key, data):
    """Populate the MORE panel with fetched data."""
    parent = rs.more_group_tag
    dpg.delete_item(parent, children_only=True)
    dpg.configure_item(parent, show=True)

    has_content = False

    # Full latest commit message (subject + body). The header shows only the
    # subject line; the complete message is revealed here.
    if rs.last_commit_msg:
        has_content = True
        dpg.add_text("  Latest commit message:", color=COL_ACCENT, parent=parent)
        indented = "\n".join("    " + ln for ln in rs.last_commit_msg.splitlines())
        dpg.add_text(indented, color=COL_DIM, parent=parent, wrap=0)

    # A. Gitignored files
    ignored = data.get("ignored_files", [])
    if ignored:
        has_content = True
        dpg.add_text(f"  Gitignored files ({len(ignored)}):",
                     color=COL_ACCENT, parent=parent)
        for f in ignored:
            dpg.add_text(f"    {f}", color=COL_DIM, parent=parent)
    else:
        dpg.add_text("  Gitignored files: none", color=COL_DIM, parent=parent)

    # A.5 New branch (create off current HEAD and switch to it)
    rs.more_local_branches = set(data.get("local_branch_names", []))
    has_content = True
    with dpg.group(horizontal=True, parent=parent):
        dpg.add_text("  New branch:", color=COL_ACCENT)
        name_input = dpg.add_input_text(
            width=280, hint="new-branch-name", on_enter=True,
        )
        dpg.configure_item(name_input, callback=cb_create_branch,
                           user_data=(repo_key, name_input))
        create_btn = dpg.add_button(
            label="Create",
            callback=cb_create_branch,
            user_data=(repo_key, name_input),
        )
        dpg.bind_item_theme(create_btn, green_btn_theme)

    # B. Switch branch (local + remote-only)
    branches = data.get("branches", [])
    current = data.get("current_branch", "")
    rs.more_branch_targets = data.get("branch_targets", {})
    # Filter out the current branch by matching the underlying checkout target,
    # not the label (labels may carry "(local only)" / "(stale)" suffixes).
    current_target = ["checkout", current]
    other_branches = [
        b for b in branches if rs.more_branch_targets.get(b) != current_target
    ]
    if other_branches:
        has_content = True
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("  Switch branch:", color=COL_ACCENT)
            combo_tag = dpg.add_combo(
                other_branches,
                default_value=other_branches[0],
                width=280,
            )
            switch_btn = dpg.add_button(
                label="Switch",
                callback=cb_switch_branch,
                user_data=(repo_key, combo_tag),
            )
            dpg.bind_item_theme(switch_btn, link_btn_theme)
    else:
        dpg.add_text("  Switch branch: only one branch", color=COL_DIM, parent=parent)

    # B.5 Delete local-only / stale branches
    deletable = data.get("deletable", {})
    rs.more_deletable = dict(deletable)
    if deletable:
        has_content = True
        labels = list(deletable.keys())
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("  Delete branch:", color=COL_ACCENT)
            del_combo_tag = dpg.add_combo(
                labels,
                default_value=labels[0],
                width=280,
            )
            del_btn = dpg.add_button(
                label="Delete",
                callback=cb_delete_branch,
                user_data=(repo_key, del_combo_tag),
            )
            dpg.bind_item_theme(del_btn, remove_btn_theme)

    # C. Dispatch workflow
    workflows = data.get("workflows", [])
    ref = data.get("current_branch", "")
    if workflows:
        has_content = True
        dpg.add_text("  Run Workflow:", color=COL_ACCENT, parent=parent)
        for wf in workflows:
            with dpg.group(horizontal=True, parent=parent):
                dpg.add_text(f"    {wf['name']}", color=COL_DIM)
                run_btn = dpg.add_button(
                    label="Run",
                    callback=cb_dispatch_workflow,
                    user_data=(repo_key, wf["id"], wf["name"], ref),
                )
                dpg.bind_item_theme(run_btn, green_btn_theme)
    else:
        dpg.add_text("  Run Workflow: none for this branch", color=COL_DIM,
                     parent=parent)

    dpg.add_spacer(height=4, parent=parent)


def cb_switch_branch(sender, app_data, user_data):
    repo_key, combo_tag = user_data
    label = dpg.get_value(combo_tag)
    if not label:
        return
    rs = app.repos.get(repo_key)
    if not rs:
        return
    targets = getattr(rs, "more_branch_targets", {})
    args = targets.get(label, ["checkout", label])
    executor.submit(bg_switch_branch, repo_key, label, args)


def bg_switch_branch(repo_key, label, args, confirmed=False):
    """Switch to *label*, isolating any uncommitted work via a branch-tagged
    autostash. If the source tree is dirty (on a real branch) and the user
    hasn't confirmed, posts 'switch_branch_needs_confirm' first. The stash
    (including untracked files) is tagged with the source branch and
    auto-restored when the user returns to it."""
    rs = app.repos.get(repo_key)
    if not rs:
        return
    cwd = str(rs.path)
    source = get_current_branch(cwd)
    detached = source in ("", "HEAD")
    ok, entries = read_status(cwd)

    # Fail safe: if the working tree can't be read (git busy / error), a failed
    # status must NOT be mistaken for a clean tree -- that would skip the confirm
    # gate and switch without stashing. Abort and let the user retry.
    if not ok:
        ui_queue.put(("more_action_result", repo_key, False,
                      "Switch aborted: couldn't read the working tree "
                      "(git busy). Try again."))
        return

    # Confirm gate: only when there's something to stash on a real branch.
    if entries and not detached and not confirmed:
        ui_queue.put(("switch_branch_needs_confirm", repo_key, label, args,
                      source, len(entries)))
        return

    # Stash uncommitted work (incl. untracked) tagged with the source branch.
    stashed = False
    if entries and not detached:
        marker = f"ai-commit-autostash:{source}"
        rc, _, err = run_git(
            ["stash", "push", "--include-untracked", "-m", marker], cwd=cwd)
        if rc != 0:
            ui_queue.put(("more_action_result", repo_key, False,
                          f"Stash failed: {err.strip()}"))
            return
        stashed = True

    # Switch to the target branch.
    rc, _, err = run_git(args, cwd=cwd)
    if rc != 0:
        # Roll back our stash so the user is left exactly as before.
        if stashed:
            run_git(["stash", "pop"], cwd=cwd)
        ui_queue.put(("more_action_result", repo_key, False,
                      f"Switch failed: {err.strip()}"))
        return

    # Auto-restore: pop the topmost autostash for the branch we actually landed
    # on, but only onto a clean tree and never forcing a conflicted pop.
    target = get_current_branch(cwd)
    msg = f"Switched to {label}"
    rc, stash_out, _ = run_git(["stash", "list"], cwd=cwd)
    ref = find_autostash_ref(stash_out, target) if rc == 0 else None
    if ref:
        if get_status(cwd):
            msg = f"Switched to {label}; stash for {target} kept (tree not clean)"
        else:
            rc_p, _, _ = run_git(["stash", "pop", ref], cwd=cwd)
            if rc_p == 0:
                msg = f"Switched to {label} (restored stashed changes)"
            else:
                msg = (f"Switched to {label}; stashed changes kept "
                       f"(pop conflict -- resolve manually)")

    ui_queue.put(("more_action_result", repo_key, True, msg))
    bg_refresh_single_repo(repo_key)


def cb_create_branch(sender, app_data, user_data):
    """Create a new branch off current HEAD and switch to it."""
    repo_key, input_tag = user_data
    name = dpg.get_value(input_tag).strip()
    if not name:
        ui_queue.put(("more_action_result", repo_key, False,
                      "Enter a branch name"))
        return
    rs = app.repos.get(repo_key)
    if not rs:
        return
    if name in getattr(rs, "more_local_branches", set()):
        ui_queue.put(("more_action_result", repo_key, False,
                      f"Branch '{name}' already exists"))
        return
    executor.submit(bg_create_branch, repo_key, name, False)


def bg_create_branch(repo_key, name, confirmed):
    """Create branch `name` off HEAD via `git switch -c` and switch to it.
    Checks the live working tree first; if there are uncommitted changes and
    the user hasn't confirmed yet, posts 'create_branch_needs_confirm' so the
    UI can warn before proceeding (the changes ride along to the new branch)."""
    rs = app.repos.get(repo_key)
    if not rs:
        return
    cwd = str(rs.path)
    ok, entries = read_status(cwd)
    if not ok:
        ui_queue.put(("more_action_result", repo_key, False,
                      "Create aborted: couldn't read the working tree "
                      "(git busy). Try again."))
        return
    if entries and not confirmed:
        ui_queue.put(("create_branch_needs_confirm", repo_key, name, len(entries)))
        return
    rc, stdout, stderr = run_git(["switch", "-c", name], cwd=cwd)
    if rc == 0:
        ui_queue.put(("more_action_result", repo_key, True,
                      f"Created and switched to '{name}'"))
        bg_refresh_single_repo(repo_key)
    else:
        ui_queue.put(("more_action_result", repo_key, False,
                      f"Create failed: {stderr.strip()}"))


def cb_delete_branch(sender, app_data, user_data):
    """Delete the selected local-only / stale branch (from the Delete combo)."""
    repo_key, combo_tag = user_data
    label = dpg.get_value(combo_tag)
    if not label:
        return
    rs = app.repos.get(repo_key)
    if not rs:
        return
    deletable = getattr(rs, "more_deletable", {})
    branch_name = deletable.get(label)
    if not branch_name:
        # Combo value isn't in the deletable set (shouldn't happen via UI).
        ui_queue.put(("more_action_result", repo_key, False,
                      f"Refusing to delete '{label}' (not local-only or stale)"))
        return
    executor.submit(bg_delete_branch, repo_key, branch_name, False)


def bg_delete_branch(repo_key, branch_name, force):
    """Delete a local branch. Tries safe (-d) first; if that refuses because
    of unmerged work, posts a 'delete_branch_needs_force' message so the UI
    can prompt for force-delete confirmation."""
    rs = app.repos.get(repo_key)
    if not rs:
        return
    flag = "-D" if force else "-d"
    rc, stdout, stderr = run_git(["branch", flag, branch_name], cwd=str(rs.path))
    if rc == 0:
        ui_queue.put(("more_action_result", repo_key, True,
                      f"Deleted branch '{branch_name}'"))
        bg_refresh_single_repo(repo_key)
        return
    err = stderr.strip() or stdout.strip()
    if not force and "not fully merged" in err.lower():
        ui_queue.put(("delete_branch_needs_force", repo_key, branch_name, err))
        return
    ui_queue.put(("more_action_result", repo_key, False,
                  f"Delete failed: {err}"))


def cb_dispatch_workflow(sender, app_data, user_data):
    repo_key, wf_id, wf_name, ref = user_data
    executor.submit(bg_dispatch_workflow, repo_key, wf_id, wf_name, ref)


def bg_dispatch_workflow(repo_key, wf_id, wf_name, ref):
    rs = app.repos.get(repo_key)
    if not rs:
        return
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        # Captured before dispatch so any run created at/after this moment
        # is ours; small backoff covers clock skew vs GitHub.
        after_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cmd = ["gh", "workflow", "run", str(wf_id)]
        if ref:
            cmd += ["--ref", ref]
        result = subprocess.run(
            cmd,
            cwd=str(rs.path),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30, **kwargs,
        )
        if result.returncode == 0:
            ui_queue.put(("more_action_result", repo_key, True,
                          f"Dispatched '{wf_name}'"))
            if app.actions_popup_enabled and rs.remote_url:
                executor.submit(
                    _launch_workflow_viewer_dispatch,
                    repo_key, wf_id, wf_name, after_iso,
                )
        else:
            err = result.stderr.strip() or result.stdout.strip()
            ui_queue.put(("more_action_result", repo_key, False,
                          f"Dispatch failed: {err}"))
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        ui_queue.put(("more_action_result", repo_key, False, str(exc)))


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


def _non_git_for_rebuild():
    """Return the current non-git folders as a dict suitable for rebuild_repos_ui."""
    return {k: {"path": ngf.path, "name": ngf.name, "mtime": ngf.mtime}
            for k, ngf in app.non_git_folders.items()}


def rebuild_repos_ui(results, non_git_results=None, clear_errors=False,
                     preserve_open=False):
    """Rebuild repo sections from poll results.

    If a repo's file list changed since last poll, its pending commit message
    is erased (and auto-generated again if that setting is on).  If the files
    are unchanged, the existing message is preserved.

    *preserve_open* keeps each header's current expand/collapse state across the
    rebuild. It is True for partial rebuilds (single-repo refresh / refresh-then-
    generate) so repos are not auto-collapsed, and False for full refreshes (the
    automatic poll loop and manual Refresh-all), which reapply the activity
    default. See compute_header_open() in ai_commit_core.
    """
    # Capture current open/collapse state before the headers are destroyed so a
    # partial rebuild can restore it (keyed by path).
    prior_open = {}
    if preserve_open:
        for rs in app.repos.values():
            if rs.header_tag and dpg.does_item_exist(rs.header_tag):
                prior_open[str(rs.path)] = dpg.get_value(rs.header_tag)
        for ngf in app.non_git_folders.values():
            if ngf.header_tag and dpg.does_item_exist(ngf.header_tag):
                prior_open[str(ngf.path)] = dpg.get_value(ngf.header_tag)

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
    def _display_sort_key(item):
        _, info = item
        if app.sort_by_date:
            has_changes = 0 if info.get("entries") else 1
            return (has_changes, -info.get("last_commit_ts", 0.0))
        git_name = _repo_name_from_url(info.get("remote_url", ""))
        return (git_name or info["path"].name).lower()

    for name, info in sorted(results.items(), key=_display_sort_key):
        old_rs = app.repos.get(name)
        new_entries = info["entries"]

        # Detect whether files changed since last poll
        files_changed = True
        if old_rs is not None:
            files_changed = (old_rs.entries != new_entries)

        if new_entries or info.get("behind", 0) > 0:
            any_changes = True

        # Decide what to keep
        if name in preserved:
            prev_msg, prev_gen, prev_err = preserved[name]
            # Sticky errors survive rebuilds (cleared by manual Refresh
            # or automatic polling when repo is force-active)
            repo_force_active = app.repo_overrides.get(name, "") == "active"
            if prev_gen == GenStatus.ERROR and not clear_errors and not repo_force_active:
                msg, gen, err = prev_msg, prev_gen, prev_err
            elif prev_gen == GenStatus.GENERATING:
                msg, gen, err = prev_msg, prev_gen, prev_err
            elif files_changed:
                msg, gen, err = "", GenStatus.IDLE, ""
            else:
                msg, gen, err = prev_msg, (GenStatus.DONE if prev_msg else GenStatus.IDLE), prev_err
        else:
            msg, gen, err = "", GenStatus.IDLE, ""

        folder_name = info["path"].name
        git_name = _repo_name_from_url(info.get("remote_url", ""))
        display_name = git_name if git_name else folder_name
        rs = RepoState(
            path=info["path"],
            name=display_name,
            folder_name=folder_name,
            entries=new_entries,
            commit_message=msg,
            gen_status=gen,
            error_message=err,
            remote_url=info.get("remote_url", ""),
            github_account=info.get("github_account", ""),
            visibility=info.get("visibility", ""),
            branch=info.get("branch", ""),
            branch_status=info.get("branch_status", ""),
            last_commit_msg=info.get("last_commit_msg", ""),
            last_commit_date=info.get("last_commit_date", ""),
            last_commit_ts=info.get("last_commit_ts", 0.0),
            ahead=info.get("ahead", 0),
            behind=info.get("behind", 0),
        )
        new_repos[name] = rs

    # Build non-git folder entries
    new_non_git = {}
    if non_git_results:
        for key, info in non_git_results.items():
            new_non_git[key] = NonGitFolder(path=info["path"], name=info["name"],
                                            mtime=info.get("mtime", 0.0))

    # Disambiguate duplicate display names across both git repos and non-git
    # folders by prefixing with the parent folder name (e.g. "ClaudeCode/foo").
    # Same prefix is applied to folder_name so the folder/repo-name mismatch
    # marker keeps working correctly.
    name_counts = {}
    for rs in new_repos.values():
        name_counts[rs.name] = name_counts.get(rs.name, 0) + 1
    for ngf in new_non_git.values():
        name_counts[ngf.name] = name_counts.get(ngf.name, 0) + 1
    for rs in new_repos.values():
        if name_counts.get(rs.name, 0) > 1:
            prefix = f"{rs.path.parent.name}/"
            rs.name = prefix + rs.name
            rs.folder_name = prefix + rs.folder_name
    for ngf in new_non_git.values():
        if name_counts.get(ngf.name, 0) > 1:
            ngf.name = f"{ngf.path.parent.name}/{ngf.name}"

    # Compute max base-label width so dates right-align
    label_width = max((len(_repo_base_label(rs)) for rs in new_repos.values()), default=0)

    # Render git repos first (sorted), then non-git folders at the bottom.
    # When "Recent only" is on, hide idle repos (clean, synced, last commit older
    # than recent_days) -- but always keep force-active repos and repos with a
    # sticky error visible. Hidden repos stay in app.repos (state/polling continue).
    now = time.time()
    hidden_count = 0
    for rs in sorted(new_repos.values(), key=lambda r: (0 if r.entries else 1, -r.last_commit_ts) if app.sort_by_date else r.name.lower()):
        if app.recent_only:
            repo_force_active = app.repo_overrides.get(str(rs.path), "") == "active"
            sticky_error = rs.gen_status == GenStatus.ERROR
            if (not repo_force_active and not sticky_error
                    and not is_repo_active(rs.last_commit_ts, bool(rs.entries),
                                           rs.ahead, rs.behind, now, app.recent_days)):
                hidden_count += 1
                continue
        build_repo_section(rs, "repos_container", label_width=label_width,
                           preserve_open=preserve_open, prior_open=prior_open)

    # Non-git folders get the same recency filter, using the folder's
    # modification time in place of git signals (no commits/dirty state to go
    # on). Hidden folders stay in app.non_git_folders so toggling re-shows them.
    if app.show_non_git_folders:
        for ngf in sorted(new_non_git.values(), key=lambda n: -n.mtime if app.sort_by_date else str(n.path).lower()):
            if app.recent_only and not is_folder_recent(ngf.mtime, now,
                                                        app.recent_days):
                hidden_count += 1
                continue
            build_non_git_section(ngf, "repos_container",
                                  preserve_open=preserve_open, prior_open=prior_open)
    if dpg.does_item_exist("hidden_count_label"):
        dpg.set_value("hidden_count_label",
                      f"{hidden_count} hidden" if (app.recent_only and hidden_count) else "")

    app.repos = new_repos
    app.non_git_folders = new_non_git

    # Auto-generate for repos with changes and no message
    for name, rs in app.repos.items():
        if rs.entries and not rs.commit_message and rs.gen_status == GenStatus.IDLE:
            if app.auto_generate:
                rs.gen_status = GenStatus.GENERATING
                update_repo_status(rs)
                executor.submit(bg_generate_message, name)

    # Update tray alert based on whether any repos have changes,
    # regardless of whether the main window is visible.
    _set_tray_alert(any_changes)


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

        if kind == "active_gh_account":
            app.active_gh_account = msg[1]

        elif kind == "poll_result":
            results = msg[1]
            non_git = msg[2] if len(msg) > 2 else {}
            clear_errors = msg[3] if len(msg) > 3 else False
            # Cache the raw payload so the "Recent only" toggle can re-render the
            # list (applying/removing the filter) without spawning a poll.
            app.last_results = results
            app.last_non_git = non_git
            rebuild_repos_ui(results, non_git, clear_errors=clear_errors)

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
                rs.gen_entries = list(rs.entries)
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    display = _wrap_for_display(message)
                    dpg.set_value(rs.input_tag, display)
                    dpg.configure_item(rs.input_tag, height=_height_for_text(display))
            update_repo_status(rs)

        elif kind == "commit_result":
            _, repo_name, committed, pushed, detail = msg
            rs = app.repos.get(repo_name)
            if not rs:
                continue
            if not committed and detail == "STALE":
                rs.gen_status = GenStatus.ERROR
                rs.error_message = "Files changed since message was generated - please regenerate"
                update_repo_status(rs)
            elif committed and pushed:
                rs.gen_status = GenStatus.IDLE
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                dpg.set_value(rs.status_tag, "Committed & pushed!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                # Fully synced now -- let the upcoming partial rebuild re-apply
                # the activity default so this (now idle) repo collapses instead
                # of staying expanded under preserve_open.
                app.collapse_on_next_build.add(repo_name)
                executor.submit(bg_refresh_single_repo, repo_name)
                if app.actions_popup_enabled and rs.remote_url:
                    executor.submit(_launch_workflow_viewer, repo_name, rs)
            elif committed and not pushed and detail == "LOCAL_ONLY":
                rs.gen_status = GenStatus.IDLE
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                dpg.set_value(rs.status_tag, "Committed!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                app.collapse_on_next_build.add(repo_name)
                executor.submit(bg_refresh_single_repo, repo_name)
            elif committed and not pushed and detail.startswith("NO_UPSTREAM:"):
                branch = detail.split(":", 1)[1]
                rs.gen_status = GenStatus.IDLE
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                dpg.set_value(rs.status_tag, f"No upstream for {branch} -- set up tracking?")
                dpg.configure_item(rs.status_tag, color=COL_YELLOW)
                _show_upstream_prompt(repo_name, branch)
            elif committed and not pushed:
                rs.gen_status = GenStatus.ERROR
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                rs.error_message = detail
                update_repo_status(rs)
                # GitLab secret push protection block -- the commit landed, so
                # offer a one-time `-o secret_push_protection.skip_all` retry.
                if is_secret_push_block(detail):
                    _show_secret_push_prompt(repo_name)
            else:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = detail
                update_repo_status(rs)

        elif kind == "push_upstream_result":
            _, repo_name, success, detail = msg[:4]
            upstream_branch = msg[4] if len(msg) > 4 else ""
            rs = app.repos.get(repo_name)
            if not rs:
                continue
            if success:
                rs.gen_status = GenStatus.IDLE
                dpg.set_value(rs.status_tag, "Committed & pushed!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                # Fully synced now -- collapse this idle repo on the next rebuild.
                app.collapse_on_next_build.add(repo_name)
                executor.submit(bg_refresh_single_repo, repo_name)
                if app.actions_popup_enabled and rs.remote_url:
                    executor.submit(_launch_workflow_viewer, repo_name, rs)
            else:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = f"Push failed: {detail}"
                update_repo_status(rs)
                # Blocked by secret push protection -- retry must keep the
                # --set-upstream since the branch still has no tracking ref.
                if is_secret_push_block(detail):
                    _show_secret_push_prompt(repo_name, upstream_branch)

        elif kind == "workflow_check":
            _, repo_name, reason = msg
            rs = app.repos.get(repo_name)
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                if reason == "no_runs":
                    text = "Pushed - no Actions runs triggered for this commit"
                elif reason == "no_token":
                    text = "Pushed - Actions check skipped (no gh CLI token)"
                elif reason == "no_remote":
                    text = "Pushed - Actions check skipped (no remote/SHA)"
                else:
                    text = ""
                if text:
                    dpg.set_value(rs.status_tag, text)
                    dpg.configure_item(rs.status_tag, color=COL_DIM)

        elif kind == "repo_loading":
            _, repo_key, repo_display_name = msg
            # Show a loading indicator for this repo if it already exists
            rs = app.repos.get(repo_key)
            if rs and rs.header_tag and dpg.does_item_exist(rs.header_tag):
                old_label = dpg.get_item_label(rs.header_tag)
                if not old_label.endswith(" ..."):
                    dpg.configure_item(rs.header_tag, label=old_label + "  ...")
            elif dpg.does_item_exist("repos_container"):
                # New repo being discovered -- show placeholder
                dpg.add_text(
                    f"  {repo_display_name}  ...",
                    color=COL_DIM, parent="repos_container",
                )

        elif kind == "single_repo_refresh":
            _, repo_name, info = msg[:3]
            force = msg[3] if len(msg) > 3 else False
            # A manual per-repo Refresh (force=True) acknowledges this repo's
            # sticky error, same as Refresh-all does via clear_errors. Clear it
            # before the rebuild snapshots gen_status/error_message, or the
            # sticky-error branch in rebuild_repos_ui keeps it alive.
            rs = app.repos.get(repo_name)
            if force and rs and rs.gen_status == GenStatus.ERROR:
                rs.gen_status = GenStatus.IDLE
                rs.error_message = ""
            # Merge fresh data for this repo into current state and rebuild
            merged = {}
            for name, rs in app.repos.items():
                merged[name] = {
                    "path": rs.path,
                    "entries": rs.entries,
                    "remote_url": rs.remote_url,
                    "github_account": rs.github_account,
                    "visibility": rs.visibility,
                    "branch": rs.branch,
                    "branch_status": rs.branch_status,
                    "last_commit_msg": rs.last_commit_msg,
                    "last_commit_date": rs.last_commit_date,
                    "last_commit_ts": rs.last_commit_ts,
                    "ahead": rs.ahead,
                    "behind": rs.behind,
                }
            merged[repo_name] = info
            # Keep the no-poll re-render cache in sync with what we just rendered,
            # or the Date/Recent toggles (which re-render from app.last_results)
            # resurrect this repo's stale pre-refresh entries.
            app.last_results = merged
            app.last_non_git = _non_git_for_rebuild()
            rebuild_repos_ui(merged, _non_git_for_rebuild(), preserve_open=True)

        elif kind == "refresh_then_generate":
            _, repo_name, info = msg
            # Merge fresh data and rebuild UI
            merged = {}
            for name, rs in app.repos.items():
                merged[name] = {
                    "path": rs.path,
                    "entries": rs.entries,
                    "remote_url": rs.remote_url,
                    "github_account": rs.github_account,
                    "visibility": rs.visibility,
                    "branch": rs.branch,
                    "branch_status": rs.branch_status,
                    "last_commit_msg": rs.last_commit_msg,
                    "last_commit_date": rs.last_commit_date,
                    "last_commit_ts": rs.last_commit_ts,
                    "ahead": rs.ahead,
                    "behind": rs.behind,
                }
            merged[repo_name] = info
            # Keep the no-poll re-render cache in sync (see single_repo_refresh).
            app.last_results = merged
            app.last_non_git = _non_git_for_rebuild()
            rebuild_repos_ui(merged, _non_git_for_rebuild(), preserve_open=True)
            # Now kick off generation if there are still changes
            rs = app.repos.get(repo_name)
            if rs and rs.entries:
                rs.gen_status = GenStatus.GENERATING
                rs.error_message = ""
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                update_repo_status(rs)
                executor.submit(bg_generate_message, repo_name)
            elif rs:
                rs.gen_status = GenStatus.IDLE
                update_repo_status(rs)

        elif kind == "create_remote_result":
            _, repo_name, ok, detail = msg
            rs = app.repos.get(repo_name)
            if not rs:
                continue
            if ok:
                rs.remote_url = detail
                dpg.set_value(rs.status_tag, "GitHub repo created!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                # Rebuild to show GitHub button instead of Create Remote
                executor.submit(bg_refresh_single_repo, repo_name)
            else:
                dpg.set_value(rs.status_tag, f"Create failed: {detail}")
                dpg.configure_item(rs.status_tag, color=COL_RED)

        elif kind == "gh_accounts_result":
            _, repo_key, accounts, active_account, click_pos = msg
            rs = app.repos.get(repo_key)
            if rs:
                dpg.set_value(rs.status_tag, "")
                _show_create_remote_popup(
                    repo_key, accounts, active_account, click_pos)

        elif kind == "preview_pull_result":
            _, repo_name, commits, diffstat = msg
            rs = app.repos.get(repo_name)
            if rs:
                if not commits and not diffstat:
                    dpg.set_value(rs.status_tag, "No incoming changes found")
                    dpg.configure_item(rs.status_tag, color=COL_DIM)
                else:
                    dpg.set_value(rs.status_tag, "Preview ready")
                    dpg.configure_item(rs.status_tag, color=COL_GREEN)
                    # Show preview window
                    repo_key = str(rs.path)
                    win_tag = dpg.generate_uuid()
                    with dpg.window(
                        label=f"Incoming changes - {rs.name}",
                        tag=win_tag,
                        width=620, height=420,
                        no_collapse=True,
                        on_close=lambda s, a, u: (
                            dpg.delete_item(s) if dpg.does_item_exist(s) else None
                        ),
                    ):
                        if commits:
                            dpg.add_text("Commits:", color=COL_ACCENT)
                            dpg.add_input_text(
                                default_value=commits,
                                multiline=True, readonly=True,
                                width=-1, height=140,
                            )
                            dpg.add_spacer(height=6)
                        if diffstat:
                            dpg.add_text("Files changed:", color=COL_ACCENT)
                            dpg.add_input_text(
                                default_value=diffstat,
                                multiline=True, readonly=True,
                                width=-1, height=140,
                            )
                            dpg.add_spacer(height=6)
                        with dpg.group(horizontal=True):
                            pull_btn = dpg.add_button(
                                label="Pull Now",
                                callback=cb_confirm_pull,
                                user_data=(repo_key, win_tag),
                            )
                            dpg.bind_item_theme(pull_btn, pull_btn_theme)
                            dpg.add_button(
                                label="Cancel",
                                callback=cb_close_preview,
                                user_data=win_tag,
                            )

        elif kind == "pull_result":
            _, repo_name, ok, detail = msg
            rs = app.repos.get(repo_name)
            if rs:
                if ok:
                    dpg.set_value(rs.status_tag, "Pulled successfully!")
                    dpg.configure_item(rs.status_tag, color=COL_GREEN)
                    executor.submit(bg_refresh_single_repo, repo_name)
                else:
                    dpg.set_value(rs.status_tag, f"Pull failed: {detail}")
                    dpg.configure_item(rs.status_tag, color=COL_RED)

        elif kind == "clean_preview_result":
            _, repo_name, ok, output = msg
            rs = app.repos.get(repo_name)
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                if not ok:
                    dpg.set_value(rs.status_tag, f"Clean preview failed: {output}")
                    dpg.configure_item(rs.status_tag, color=COL_RED)
                elif not output.strip():
                    dpg.set_value(rs.status_tag, "Already clean - nothing to remove")
                    dpg.configure_item(rs.status_tag, color=COL_GREEN)
                    threading.Timer(
                        2.5,
                        lambda r=repo_name: ui_queue.put(("clean_clear_status", r)),
                    ).start()
                else:
                    dpg.set_value(rs.status_tag, "Clean preview ready")
                    dpg.configure_item(rs.status_tag, color=COL_GREEN)
                    repo_key = str(rs.path)
                    win_tag = dpg.generate_uuid()
                    with dpg.window(
                        label=f"Clean preview - {rs.name}",
                        tag=win_tag,
                        width=620, height=420,
                        no_collapse=True,
                        on_close=lambda s, a, u: (
                            dpg.delete_item(s) if dpg.does_item_exist(s) else None
                        ),
                    ):
                        dpg.add_text(
                            "These untracked files/folders would be removed by 'git clean -fd':",
                            color=COL_ACCENT,
                        )
                        dpg.add_input_text(
                            default_value=output,
                            multiline=True, readonly=True,
                            width=-1, height=280,
                        )
                        dpg.add_spacer(height=6)
                        dpg.add_text("This action cannot be undone.", color=COL_RED)
                        dpg.add_spacer(height=6)
                        with dpg.group(horizontal=True):
                            clean_btn = dpg.add_button(
                                label="Clean Now",
                                callback=cb_confirm_clean,
                                user_data=(repo_key, win_tag),
                            )
                            dpg.bind_item_theme(clean_btn, pull_btn_theme)
                            dpg.add_button(
                                label="Cancel",
                                callback=cb_close_clean_preview,
                                user_data=win_tag,
                            )

        elif kind == "clean_clear_status":
            _, repo_name = msg
            rs = app.repos.get(repo_name)
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                update_repo_status(rs)

        elif kind == "clean_result":
            _, repo_name, ok, removed, errors = msg
            rs = app.repos.get(repo_name)
            trigger_poll()
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                if ok:
                    n = len(removed)
                    dpg.set_value(rs.status_tag, f"Removed {n} item{'s' if n != 1 else ''}")
                    dpg.configure_item(rs.status_tag, color=COL_GREEN)
                else:
                    dpg.set_value(
                        rs.status_tag,
                        f"Clean: removed {len(removed)}, {len(errors)} failed",
                    )
                    dpg.configure_item(rs.status_tag, color=COL_RED)
                    win_tag = dpg.generate_uuid()
                    with dpg.window(
                        label=f"Clean result - {rs.name}",
                        tag=win_tag,
                        width=620, height=420,
                        no_collapse=True,
                        on_close=lambda s, a, u: (
                            dpg.delete_item(s) if dpg.does_item_exist(s) else None
                        ),
                    ):
                        if removed:
                            dpg.add_text(
                                f"Removed ({len(removed)}):", color=COL_GREEN)
                            dpg.add_input_text(
                                default_value="\n".join(removed),
                                multiline=True, readonly=True,
                                width=-1, height=140,
                            )
                            dpg.add_spacer(height=6)
                        if errors:
                            dpg.add_text(
                                f"Failed ({len(errors)}):", color=COL_RED)
                            dpg.add_input_text(
                                default_value="\n".join(errors),
                                multiline=True, readonly=True,
                                width=-1, height=140,
                            )
                            dpg.add_spacer(height=6)
                            dpg.add_text(
                                "Tip: 'Permission denied' usually means a process holds an "
                                "open handle (dev server, editor, OneDrive). Close it and retry.",
                                color=COL_DIM, wrap=580,
                            )
                        dpg.add_spacer(height=6)
                        dpg.add_button(
                            label="Close",
                            callback=cb_close_clean_preview,
                            user_data=win_tag,
                        )

        elif kind == "folder_selected":
            chosen = msg[1]
            folder = Path(chosen).resolve()
            if folder.is_dir() and folder not in app.watched_folders:
                app.watched_folders.append(folder)
                _save_settings()
                _rebuild_folders_ui()
                trigger_poll()

        elif kind == "git_init_result":
            _, folder_path, ok, detail = msg
            if ok:
                trigger_poll()
            else:
                ngf = app.non_git_folders.get(folder_path)
                if ngf and ngf.status_tag and dpg.does_item_exist(ngf.status_tag):
                    dpg.set_value(ngf.status_tag, f"Init failed: {detail}")
                    dpg.configure_item(ngf.status_tag, color=COL_RED)

        elif kind == "more_data_result":
            _, repo_key, more_data = msg
            rs = app.repos.get(repo_key)
            if rs and rs.more_group_tag and dpg.does_item_exist(rs.more_group_tag):
                _build_more_panel(rs, repo_key, more_data)

        elif kind == "more_action_result":
            _, repo_key, ok, detail = msg
            rs = app.repos.get(repo_key)
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                dpg.set_value(rs.status_tag, detail)
                dpg.configure_item(rs.status_tag, color=COL_GREEN if ok else COL_RED)

        elif kind == "delete_branch_needs_force":
            _, repo_key, branch_name, err = msg
            rs = app.repos.get(repo_key)
            if not rs:
                continue
            win_tag = dpg.generate_uuid()
            with dpg.window(
                label=f"Force delete branch?",
                tag=win_tag,
                width=460, height=180,
                no_collapse=True, modal=True,
                on_close=lambda s, a, u: (
                    dpg.delete_item(s) if dpg.does_item_exist(s) else None
                ),
            ):
                dpg.add_text(
                    f"Branch '{branch_name}' has unmerged commits.",
                    color=COL_YELLOW,
                )
                dpg.add_text(err, color=COL_DIM, wrap=420)
                dpg.add_spacer(height=8)
                dpg.add_text(
                    "Force delete will discard those commits permanently.",
                    color=COL_RED,
                )
                dpg.add_spacer(height=8)
                with dpg.group(horizontal=True):
                    force_btn = dpg.add_button(
                        label="Force Delete",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u[2]) if dpg.does_item_exist(u[2]) else None,
                            executor.submit(bg_delete_branch, u[0], u[1], True),
                        ),
                        user_data=(repo_key, branch_name, win_tag),
                    )
                    dpg.bind_item_theme(force_btn, remove_btn_theme)
                    dpg.add_button(
                        label="Cancel",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u) if dpg.does_item_exist(u) else None
                        ),
                        user_data=win_tag,
                    )

        elif kind == "create_branch_needs_confirm":
            _, repo_key, name, n = msg
            rs = app.repos.get(repo_key)
            if not rs:
                continue
            win_tag = dpg.generate_uuid()
            with dpg.window(
                label="Create branch with uncommitted changes?",
                tag=win_tag,
                width=460, height=160,
                no_collapse=True, modal=True,
                on_close=lambda s, a, u: (
                    dpg.delete_item(s) if dpg.does_item_exist(s) else None
                ),
            ):
                dpg.add_text(
                    f"You have {n} uncommitted change(s).",
                    color=COL_YELLOW,
                )
                dpg.add_text(
                    f"They will move with you to new branch '{name}'.",
                    color=COL_DIM, wrap=420,
                )
                dpg.add_spacer(height=8)
                with dpg.group(horizontal=True):
                    proceed_btn = dpg.add_button(
                        label="Create & switch",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u[2]) if dpg.does_item_exist(u[2]) else None,
                            executor.submit(bg_create_branch, u[0], u[1], True),
                        ),
                        user_data=(repo_key, name, win_tag),
                    )
                    dpg.bind_item_theme(proceed_btn, green_btn_theme)
                    dpg.add_button(
                        label="Cancel",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u) if dpg.does_item_exist(u) else None
                        ),
                        user_data=win_tag,
                    )

        elif kind == "switch_branch_needs_confirm":
            _, repo_key, label, args, source, n = msg
            rs = app.repos.get(repo_key)
            if not rs:
                continue
            win_tag = dpg.generate_uuid()
            with dpg.window(
                label="Switch branch -- stash uncommitted changes?",
                tag=win_tag,
                width=480, height=170,
                no_collapse=True, modal=True,
                on_close=lambda s, a, u: (
                    dpg.delete_item(s) if dpg.does_item_exist(s) else None
                ),
            ):
                dpg.add_text(
                    f"You have {n} uncommitted change(s) on '{source}'.",
                    color=COL_YELLOW,
                )
                dpg.add_text(
                    f"They'll be stashed (including untracked files) and "
                    f"restored automatically when you return to '{source}'.",
                    color=COL_DIM, wrap=440,
                )
                dpg.add_spacer(height=8)
                with dpg.group(horizontal=True):
                    proceed_btn = dpg.add_button(
                        label="Stash & switch",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u[3]) if dpg.does_item_exist(u[3]) else None,
                            executor.submit(bg_switch_branch, u[0], u[1], u[2], True),
                        ),
                        user_data=(repo_key, label, args, win_tag),
                    )
                    dpg.bind_item_theme(proceed_btn, green_btn_theme)
                    dpg.add_button(
                        label="Cancel",
                        callback=lambda s, a, u: (
                            dpg.delete_item(u) if dpg.does_item_exist(u) else None
                        ),
                        user_data=win_tag,
                    )

        elif kind == "tray_show":
            _show_window()

        elif kind == "tray_quit":
            dpg.stop_dearpygui()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="AI Commit Monitor GUI")
    parser.add_argument("folder", nargs="*",
                        help="Folder(s) containing git repos to monitor")
    parser.add_argument("--provider", default=os.environ.get("AI_COMMIT_PROVIDER", "ollama"),
                        choices=["kiro", "ollama"],
                        help="AI provider (default: ollama)")
    parser.add_argument("--model", default=os.environ.get("AI_COMMIT_MODEL", "qwen3-coder:480b-cloud"),
                        help="Model name (default: qwen3-coder:480b-cloud)")
    parser.add_argument("--url", default=os.environ.get("AI_COMMIT_URL", "http://localhost:11434"),
                        help="Ollama base URL (only used with --provider ollama)")
    parser.add_argument("--poll", type=int, default=30,
                        help="Poll interval in seconds (default: 30)")
    parser.add_argument("--topmost", action="store_true",
                        help="Start with always-on-top enabled")
    parser.add_argument("--no-detach", action="store_true",
                        help="Keep attached to the launching terminal (for debugging)")
    return parser.parse_args()


green_btn_theme = None
orange_btn_theme = None
link_btn_theme = None
remove_btn_theme = None
pull_btn_theme = None


_lock_fh = None


def _acquire_instance_lock():
    """Ensure only one copy of the app runs. Exit if another is already running."""
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        print("AI Commit Monitor is already running.", file=sys.stderr)
        sys.exit(0)


def main():
    global green_btn_theme, orange_btn_theme, link_btn_theme, remove_btn_theme, pull_btn_theme, _pending_topmost

    _acquire_instance_lock()
    args = parse_args()

    # Start a fresh activity log for this session and route every git command
    # run under the hood through it so the Activity Log viewer can show them live.
    activity_log.clear_file()
    activity_log.install_git_logger()
    activity_log.log_event("ai-commit GUI started")

    app.model = args.model
    app.provider = args.provider
    app.ollama_url = args.url
    folders_from_cli = bool(args.folder)

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
        if "provider" in saved:
            app.provider = saved["provider"]
        if "ollama_url" in saved:
            app.ollama_url = saved["ollama_url"]
        app.actions_popup_enabled = saved.get("actions_popup_enabled", True)
        app.chime_on_completion = saved.get("chime_on_completion", False)
        app.show_non_git_folders = saved.get("show_non_git_folders", True)
        app.recent_only = saved.get("recent_only", True)
        app.recent_days = saved.get("recent_days", 14)
        app.idle_poll_interval = saved.get("idle_poll_interval", 900)
        app.repo_overrides = saved.get("repo_overrides", {})
        app.sort_by_date = saved.get("sort_by_date", False)
        if not folders_from_cli:
            # Support new list format and migrate old single-folder format
            saved_folders = saved.get("watched_folders", [])
            if not saved_folders and "watched_folder" in saved:
                saved_folders = [saved["watched_folder"]]
            for f in saved_folders:
                p = Path(f)
                if p.is_dir() and p not in app.watched_folders:
                    app.watched_folders.append(p)

    # CLI folder arguments take priority over saved settings
    if folders_from_cli:
        app.watched_folders = []
        for f in args.folder:
            p = Path(f).resolve()
            if p.is_dir() and p not in app.watched_folders:
                app.watched_folders.append(p)
    if not app.watched_folders:
        app.watched_folders = [Path(".").resolve()]
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
    orange_btn_theme = create_button_theme((200, 130, 30))
    pull_btn_theme = create_button_theme((200, 60, 60))

    # Link-styled button theme: transparent background, accent-colored text
    with dpg.theme() as link_btn_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (100, 140, 230, 40))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (100, 140, 230, 80))
            dpg.add_theme_color(dpg.mvThemeCol_Text, COL_ACCENT)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 2)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 2)

    # Pause-active button theme (red background)
    with dpg.theme(tag="pause_active_theme"):
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (180, 40, 40))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (200, 60, 60))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (220, 80, 80))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))

    # Force-pause header theme (red-tinted)
    with dpg.theme(tag="force_pause_header_theme"):
        with dpg.theme_component(dpg.mvCollapsingHeader):
            dpg.add_theme_color(dpg.mvThemeCol_Header, (80, 30, 30))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (95, 40, 40))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (110, 50, 50))

    # Force-active header theme (green-tinted)
    with dpg.theme(tag="force_active_header_theme"):
        with dpg.theme_component(dpg.mvCollapsingHeader):
            dpg.add_theme_color(dpg.mvThemeCol_Header, (30, 65, 40))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (40, 78, 50))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (50, 90, 60))

    # Public repo header theme (dark teal tint)
    with dpg.theme(tag="public_header_theme"):
        with dpg.theme_component(dpg.mvCollapsingHeader):
            dpg.add_theme_color(dpg.mvThemeCol_Header, (30, 55, 60))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (38, 68, 75))
            dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (46, 80, 90))

    # Small remove-button theme (red text, no background)
    with dpg.theme() as remove_btn_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (220, 80, 80, 40))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (220, 80, 80, 80))
            dpg.add_theme_color(dpg.mvThemeCol_Text, COL_RED)
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 2)

    # Main window
    with dpg.window(tag="primary", no_title_bar=True, no_resize=False,
                    no_move=True, no_close=True):

        # Watched folders
        dpg.add_group(tag="folders_container")

        with dpg.group(horizontal=True):
            dpg.add_button(label="Add Folder", callback=cb_browse)
            dpg.add_button(label="Refresh", callback=cb_refresh)
            dpg.add_button(label="Pause", tag="pause_btn", callback=cb_pause)
            dpg.add_button(label="Settings", callback=cb_open_settings)
            dpg.add_button(label="Activity Log", callback=cb_open_activity_log)
            dpg.add_checkbox(label="Date", tag="sort_by_date_cb",
                             default_value=app.sort_by_date, callback=cb_sort_by_date)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("Sort by date")
            dpg.add_checkbox(label="Recent", tag="recent_only_cb",
                             default_value=app.recent_only, callback=cb_recent_only)
            with dpg.tooltip(dpg.last_item()):
                dpg.add_text("Show only recently modified")
            dpg.add_text("", tag="hidden_count_label", color=COL_DIM)

        dpg.add_separator()

        # Scrollable repos container (negative height reserves space for model bar)
        with dpg.child_window(tag="repos_container", autosize_x=True,
                              height=-35, border=False):
            dpg.add_text("Scanning...", color=COL_DIM)

        # Model bar at bottom
        dpg.add_separator()
        with dpg.group(horizontal=True):
            dpg.add_text("Provider:", color=COL_DIM)
            dpg.add_combo(["kiro", "ollama"], tag="provider_combo",
                          default_value=app.provider, width=80,
                          callback=cb_provider_changed)
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

    # Let a few frames render so the native window exists
    for _ in range(10):
        dpg.render_dearpygui_frame()

    # Cache native window handle for always-on-top and tray operations
    _hwnd_ready = False
    if sys.platform == "win32":
        _cache_hwnd()
        if _hwnd:
            if app.always_on_top:
                _set_topmost(True)
            _install_drop_target()
            _hwnd_ready = True
        if _debug_mode:
            print(f"[debug] HWND={_hwnd} ready={_hwnd_ready}", flush=True)
    elif sys.platform == "darwin":
        _cache_nswindow()
        if _nswindow and app.always_on_top:
            _set_topmost(True)

    # System tray
    setup_tray()
    # Hide taskbar icon (app lives in the tray)
    if _hwnd_ready:
        _hide_taskbar_icon()

    # Build initial folders list and poll
    _rebuild_folders_ui()
    trigger_poll()

    # Render loop
    _hwnd_retry_count = 0
    _last_geom = None
    _geom_dirty_at = None
    while dpg.is_dearpygui_running():
        process_queue()

        # Retry HWND detection if it failed at startup
        if sys.platform == "win32" and not _hwnd_ready and _hwnd_retry_count < 60:
            _cache_hwnd()
            if _hwnd:
                if app.always_on_top:
                    _set_topmost(True)
                _hide_taskbar_icon()
                _install_drop_target()
                _hwnd_ready = True
            _hwnd_retry_count += 1

        # Intercept minimize → hide to tray instead
        if _hwnd and _has_tray and not _window_hidden and _user32.IsIconic(_hwnd):
            _user32.ShowWindow(_hwnd, 9)  # SW_RESTORE (undo iconic state)
            _hide_window()

        now = time.time()
        has_force_active = any(v == "active" for v in app.repo_overrides.values())
        if (not app.paused or has_force_active) and now - app.last_poll >= app.poll_interval:
            trigger_poll()

        dpg.render_dearpygui_frame()

        # Persist window geometry shortly after the user stops moving/resizing
        # (debounced so we don't write the settings file on every drag frame).
        if not _window_hidden:
            _pos = dpg.get_viewport_pos()
            _geom = (int(_pos[0]), int(_pos[1]),
                     dpg.get_viewport_width(), dpg.get_viewport_height())
            if _last_geom is None:
                _last_geom = _geom
            elif _geom != _last_geom:
                _last_geom = _geom
                _geom_dirty_at = now
            elif _geom_dirty_at is not None and now - _geom_dirty_at >= 1.0:
                _geom_dirty_at = None
                _save_settings()

        # Apply deferred macOS topmost change between frames
        if _pending_topmost is not None and _nswindow:
            try:
                _nswindow.setLevel_(3 if _pending_topmost else 0)
            except Exception:
                pass
            _pending_topmost = None

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
