#!/usr/bin/env python3
"""AI Commit Monitor GUI — Dear PyGui desktop app for monitoring git repos."""

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

from ai_commit_core import (
    STATUS_LABELS,
    KiroCliError,
    OllamaError,
    default_config,
    discover_repos,
    do_commit_and_push,
    do_pull,
    generate_message,
    get_active_github_account,
    get_current_branch,
    get_diff,
    get_git_global_user,
    get_git_user,
    get_git_user_local_override,
    get_github_account,
    get_head_sha,
    get_incoming_changes,
    get_repo_visibility,
    get_last_commit,
    get_remote_url,
    get_status,
    get_sync_status,
    is_git_repo,
    run_git,
)

from gh_workflows import detect_runs_for_commit, get_gh_token, parse_owner_repo

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

    # DwmSetWindowAttribute — dark title bar
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p,   # HWND
        ctypes.c_ulong,    # DWORD dwAttribute
        ctypes.c_void_p,   # LPCVOID pvAttribute
        ctypes.c_ulong,    # DWORD cbAttribute
    ]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


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
    git_user: str = ""
    github_account: str = ""
    local_name: str = ""
    local_email: str = ""
    effective_name: str = ""
    effective_email: str = ""
    visibility: str = ""
    branch: str = ""
    last_commit_msg: str = ""
    last_commit_date: str = ""
    ahead: int = 0
    behind: int = 0
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
    show_non_git_folders: bool = True
    active_gh_account: str = ""
    global_git_name: str = ""
    global_git_email: str = ""
    non_git_folders: dict = field(default_factory=dict)


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
            "watched_folders": [str(f) for f in app.watched_folders],
            "actions_popup_enabled": app.actions_popup_enabled,
            "show_non_git_folders": app.show_non_git_folders,
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
        # Defer to run between render frames — calling setLevel_ during a
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

def bg_poll_repos(force=False):
    """Discover repos and get status for each. Posts results to ui_queue.

    When *force* is True, bypass cached remote_url/git_user and always run a
    network fetch — same behavior as a fresh startup. Used by the manual
    Refresh button so a moved remote is picked up without restarting.
    """
    active_account = get_active_github_account()
    ui_queue.put(("active_gh_account", active_account))

    results = {}
    non_git_results = {}
    for folder in app.watched_folders:
        folder_path = Path(folder).resolve()
        if not folder_path.is_dir():
            continue
        if is_git_repo(folder_path):
            repo_paths = [folder_path]
            non_git_paths = []
        else:
            repo_paths = []
            non_git_paths = []
            for child in sorted(folder_path.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    if is_git_repo(child):
                        repo_paths.append(child)
                    else:
                        non_git_paths.append(child)
        for rp in repo_paths:
            repo_key = str(rp)
            ui_queue.put(("repo_loading", repo_key, rp.name))
            entries = get_status(rp)
            last_msg, last_date = get_last_commit(rp)
            existing = app.repos.get(repo_key)
            is_new = existing is None
            if not force and existing and existing.remote_url:
                remote_url = existing.remote_url
            else:
                remote_url = get_remote_url(rp)
            if not force and existing and existing.git_user:
                git_user = existing.git_user
            else:
                git_user = get_git_user(rp)
            github_account = get_github_account(remote_url)
            if not force and existing and existing.visibility:
                visibility = existing.visibility
            else:
                visibility = get_repo_visibility(rp) if remote_url else ""
            local_name, local_email = get_git_user_local_override(rp)
            rc_n, eff_name_raw, _ = run_git(["config", "user.name"], cwd=str(rp))
            rc_e, eff_email_raw, _ = run_git(["config", "user.email"], cwd=str(rp))
            effective_name = eff_name_raw.strip() if rc_n == 0 else ""
            effective_email = eff_email_raw.strip() if rc_e == 0 else ""
            branch = get_current_branch(rp)
            ahead, behind = get_sync_status(rp, fetch=is_new or force)
            results[repo_key] = {
                "path": rp,
                "entries": entries,
                "remote_url": remote_url,
                "git_user": git_user,
                "github_account": github_account,
                "visibility": visibility,
                "local_name": local_name,
                "local_email": local_email,
                "effective_name": effective_name,
                "effective_email": effective_email,
                "branch": branch,
                "last_commit_msg": last_msg,
                "last_commit_date": last_date,
                "ahead": ahead,
                "behind": behind,
            }
        for ngp in non_git_paths:
            ng_key = str(ngp)
            non_git_results[ng_key] = {"path": ngp, "name": ngp.name}
    ui_queue.put(("poll_result", results, non_git_results, force))


def bg_refresh_single_repo(repo_name):
    """Re-poll a single repo and post its updated info to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    rp = rs.path
    ui_queue.put(("repo_loading", repo_name, rp.name))
    entries = get_status(rp)
    last_msg, last_date = get_last_commit(rp)
    remote_url = rs.remote_url or get_remote_url(rp)
    git_user = rs.git_user or get_git_user(rp)
    github_account = get_github_account(remote_url)
    visibility = rs.visibility or (get_repo_visibility(rp) if remote_url else "")
    local_name, local_email = get_git_user_local_override(rp)
    rc_n, eff_name_raw, _ = run_git(["config", "user.name"], cwd=str(rp))
    rc_e, eff_email_raw, _ = run_git(["config", "user.email"], cwd=str(rp))
    effective_name = eff_name_raw.strip() if rc_n == 0 else ""
    effective_email = eff_email_raw.strip() if rc_e == 0 else ""
    branch = get_current_branch(rp)
    ahead, behind = get_sync_status(rp)
    ui_queue.put(("single_repo_refresh", repo_name, {
        "path": rp,
        "entries": entries,
        "remote_url": remote_url,
        "git_user": git_user,
        "github_account": github_account,
        "visibility": visibility,
        "local_name": local_name,
        "local_email": local_email,
        "effective_name": effective_name,
        "effective_email": effective_email,
        "branch": branch,
        "last_commit_msg": last_msg,
        "last_commit_date": last_date,
        "ahead": ahead,
        "behind": behind,
    }))


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
        config = {"provider": app.provider, "model": app.model, "url": app.ollama_url}
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


def _launch_workflow_viewer(repo_name, rs):
    """Check for workflow runs, then launch viewer only if any exist.

    Runs in background thread — blocks during detection polling.
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

    data = {"owner": owner, "repo": repo, "sha": sha, "token": token}
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False,
        dir=tempfile.gettempdir(),
    )
    json.dump(data, tmp)
    tmp.close()
    viewer = str(Path(__file__).resolve().parent / "gh_workflow_viewer.py")
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("python.exe"):
        pw = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.isfile(pw):
            exe = pw
    subprocess.Popen([exe, viewer, tmp.name])


def bg_commit_and_push(repo_name, message):
    """Commit and push for a repo. Posts result to ui_queue."""
    rs = app.repos.get(repo_name)
    if not rs:
        return
    try:
        committed, pushed, detail = do_commit_and_push(rs.path, message)
        ui_queue.put(("commit_result", repo_name, committed, pushed, detail))
    except Exception as exc:
        ui_queue.put(("commit_result", repo_name, False, False, str(exc)))


def bg_refresh_then_generate(repo_name):
    """Refresh repo status then generate a commit message.

    Posts a single_repo_refresh first, then proceeds to generate.
    """
    rs = app.repos.get(repo_name)
    if not rs:
        return
    rp = rs.path
    entries = get_status(rp)
    last_msg, last_date = get_last_commit(rp)
    remote_url = rs.remote_url or get_remote_url(rp)
    git_user = rs.git_user or get_git_user(rp)
    github_account = get_github_account(remote_url)
    visibility = rs.visibility or (get_repo_visibility(rp) if remote_url else "")
    local_name, local_email = get_git_user_local_override(rp)
    rc_n, eff_name_raw, _ = run_git(["config", "user.name"], cwd=str(rp))
    rc_e, eff_email_raw, _ = run_git(["config", "user.email"], cwd=str(rp))
    effective_name = eff_name_raw.strip() if rc_n == 0 else ""
    effective_email = eff_email_raw.strip() if rc_e == 0 else ""
    branch = get_current_branch(rp)
    ahead, behind = get_sync_status(rp, fetch=False)
    ui_queue.put(("refresh_then_generate", repo_name, {
        "path": rp,
        "entries": entries,
        "remote_url": remote_url,
        "git_user": git_user,
        "github_account": github_account,
        "visibility": visibility,
        "local_name": local_name,
        "local_email": local_email,
        "effective_name": effective_name,
        "effective_email": effective_email,
        "branch": branch,
        "last_commit_msg": last_msg,
        "last_commit_date": last_date,
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
    # Manual Refresh = forced poll: re-read remote_url/git_user and fetch,
    # matching what startup does. Otherwise a moved remote stays cached.
    trigger_poll(force=True)


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


def cb_show_non_git(sender, app_data):
    app.show_non_git_folders = dpg.get_value(sender)
    _save_settings()
    trigger_poll()


def cb_open_settings(sender, app_data):
    """Open the settings popup window."""
    win_tag = "settings_window"
    if dpg.does_item_exist(win_tag):
        dpg.focus_item(win_tag)
        return
    with dpg.window(
        label="Settings",
        tag=win_tag,
        width=340, height=360,
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
        if sys.platform == "win32":
            dpg.add_checkbox(label="Run at startup",
                             default_value=_is_startup_enabled(),
                             callback=cb_start_with_windows)
        dpg.add_spacer(height=6)
        dpg.add_text("Display", color=COL_ACCENT)
        dpg.add_checkbox(label="Show non-git folders",
                         default_value=app.show_non_git_folders,
                         callback=cb_show_non_git)
        dpg.add_spacer(height=10)
        save_btn = dpg.add_button(
            label="Save & Close",
            callback=lambda: (
                _save_settings(),
                dpg.delete_item("settings_window") if dpg.does_item_exist("settings_window") else None,
            ),
        )
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
            dpg.add_text("No accounts found — add one above.",
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
            stdout = "(no diff available)"
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
    app.provider = "ollama"
    dpg.set_value("model_input", _DEFAULT_MODEL)
    dpg.set_value("provider_combo", "ollama")


def cb_provider_changed(sender, app_data):
    app.provider = dpg.get_value(sender)


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

def trigger_poll(force=False):
    app.last_poll = time.time()
    # Immediately mark all existing repos as loading in the UI
    for rs in app.repos.values():
        if rs.header_tag and dpg.does_item_exist(rs.header_tag):
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
        dpg.add_text("No folders — click Add Folder", color=COL_DIM,
                      parent="folders_container")
        return
    for folder in app.watched_folders:
        with dpg.group(horizontal=True, parent="folders_container"):
            rm = dpg.add_button(label="x", callback=cb_remove_folder,
                                user_data=str(folder))
            dpg.bind_item_theme(rm, remove_btn_theme)
            dpg.add_text(str(folder), color=COL_DIM)


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
    return label


def build_repo_section(rs, parent, label_width=0):
    """Build the UI section for a single repo inside *parent*."""
    change_count = len(rs.entries)
    label = _repo_base_label(rs)
    show_account = rs.github_account and rs.github_account != app.active_gh_account
    vis_label = rs.visibility.lower() if rs.visibility else ("LOCAL" if not rs.remote_url else "")

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

    rs.header_tag = dpg.add_collapsing_header(
        label=label,
        parent=parent,
        default_open=change_count > 0 or rs.behind > 0 or rs.ahead > 0,
    )

    # Show identity mismatch when effective name/email differs from global
    mismatch_parts = []
    if rs.effective_name and rs.effective_name != app.global_git_name:
        mismatch_parts.append(f"name: {rs.effective_name}")
    if rs.effective_email and rs.effective_email != app.global_git_email:
        mismatch_parts.append(f"email: {rs.effective_email}")
    if mismatch_parts:
        dpg.add_text(
            f"  !! Using different identity: {', '.join(mismatch_parts)}",
            color=COL_YELLOW, parent=rs.header_tag)

    # Sync warning banner — prominent when behind remote
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
                dpg.add_text(f"  !! {sync_text} — PULL BEFORE EDITING !!", color=COL_RED)
                pull_btn = dpg.add_button(label="Preview Pull", callback=cb_preview_pull, user_data=repo_key)
                dpg.bind_item_theme(pull_btn, pull_btn_theme)
        else:
            dpg.add_text(f"  !! {sync_text} — PUSH REQUIRED !!", color=COL_RED, parent=rs.header_tag)

    # Folder name mismatch warning
    if rs.folder_name != rs.name:
        dpg.add_text(
            f"  ** Folder mismatch: folder is \"{rs.folder_name}\" but repo is \"{rs.name}\" **",
            color=COL_YELLOW, parent=rs.header_tag)

    # Links row: Open Folder, GitHub, More
    with dpg.group(horizontal=True, parent=rs.header_tag):
        folder_btn = dpg.add_button(
            label="Folder",
            callback=cb_open_folder, user_data=str(rs.path))
        dpg.bind_item_theme(folder_btn, link_btn_theme)
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

    # Full latest commit message on its own line
    if rs.last_commit_msg:
        if rs.last_commit_date:
            full_commit_text = f"  latest: {rs.last_commit_msg} — {rs.last_commit_date}"
        else:
            full_commit_text = f"  latest: {rs.last_commit_msg}"
        dpg.add_text(full_commit_text, color=COL_DIM, parent=rs.header_tag, wrap=0)

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

        # Status line
        rs.status_tag = dpg.add_text("", parent=rs.header_tag)
        update_repo_status(rs)

        # Buttons row
        with dpg.group(horizontal=True, parent=rs.header_tag):
            repo_key = str(rs.path)
            rs.gen_btn_tag = dpg.add_button(label="Generate", callback=cb_generate, user_data=repo_key)
            rs.accept_btn_tag = dpg.add_button(label="Commit & Push", callback=cb_accept, user_data=repo_key)
            dpg.bind_item_theme(rs.accept_btn_tag, green_btn_theme)

        dpg.add_spacer(height=4, parent=rs.header_tag)
    else:
        rs.status_tag = dpg.add_text("Clean", color=COL_DIM, parent=rs.header_tag)
        rs.input_tag = 0


def build_non_git_section(ngf, parent):
    """Build a minimal UI section for a non-git folder with an Init button."""
    ngf.header_tag = dpg.add_collapsing_header(
        label=f"{ngf.name}  (not a git repo)",
        parent=parent,
        default_open=False,
    )
    with dpg.group(horizontal=True, parent=ngf.header_tag):
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

    # B. Branches
    rc, stdout, _ = run_git(["branch", "--list"], cwd=cwd)
    branches = []
    current_branch = ""
    if rc == 0:
        for line in stdout.splitlines():
            line_s = line.strip()
            if line_s.startswith("* "):
                current_branch = line_s[2:].strip()
                branches.append(current_branch)
            elif line_s:
                branches.append(line_s)

    # C. Local config overrides (already known from rs, but re-check live)
    local_name, local_email = get_git_user_local_override(cwd)

    # D. Dispatchable workflows
    workflows = []
    if rs.remote_url:
        try:
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["gh", "workflow", "list", "--json", "name,id,state"],
                cwd=cwd,
                capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=15, **kwargs,
            )
            if result.returncode == 0 and result.stdout.strip():
                wf_list = json.loads(result.stdout)
                workflows = [
                    {"name": w["name"], "id": w["id"]}
                    for w in wf_list
                    if w.get("state") == "active"
                ]
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    ui_queue.put(("more_data_result", repo_key, {
        "ignored_files": ignored_files,
        "branches": branches,
        "current_branch": current_branch,
        "local_name": local_name,
        "local_email": local_email,
        "workflows": workflows,
    }))


def _build_more_panel(rs, repo_key, data):
    """Populate the MORE panel with fetched data."""
    parent = rs.more_group_tag
    dpg.delete_item(parent, children_only=True)
    dpg.configure_item(parent, show=True)

    has_content = False

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

    # B. Switch branch
    branches = data.get("branches", [])
    current = data.get("current_branch", "")
    if len(branches) > 1:
        has_content = True
        other_branches = [b for b in branches if b != current]
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text("  Switch branch:", color=COL_ACCENT)
            combo_tag = dpg.add_combo(
                other_branches,
                default_value=other_branches[0] if other_branches else "",
                width=200,
            )
            switch_btn = dpg.add_button(
                label="Switch",
                callback=cb_switch_branch,
                user_data=(repo_key, combo_tag),
            )
            dpg.bind_item_theme(switch_btn, link_btn_theme)
    else:
        dpg.add_text("  Switch branch: only one branch", color=COL_DIM, parent=parent)

    # C. Remove local config override
    local_name = data.get("local_name", "")
    local_email = data.get("local_email", "")
    if local_name or local_email:
        has_content = True
        parts = []
        if local_name:
            parts.append(f"name={local_name}")
        if local_email:
            parts.append(f"email={local_email}")
        with dpg.group(horizontal=True, parent=parent):
            dpg.add_text(f"  Local config: {', '.join(parts)}", color=COL_ACCENT)
            rm_btn = dpg.add_button(
                label="Remove Override",
                callback=cb_remove_local_config,
                user_data=repo_key,
            )
            dpg.bind_item_theme(rm_btn, remove_btn_theme)
    else:
        dpg.add_text("  Local config override: none", color=COL_DIM, parent=parent)

    # D. Dispatch workflow
    workflows = data.get("workflows", [])
    if workflows:
        has_content = True
        dpg.add_text("  Run Workflow:", color=COL_ACCENT, parent=parent)
        for wf in workflows:
            with dpg.group(horizontal=True, parent=parent):
                dpg.add_text(f"    {wf['name']}", color=COL_DIM)
                run_btn = dpg.add_button(
                    label="Run",
                    callback=cb_dispatch_workflow,
                    user_data=(repo_key, wf["id"], wf["name"]),
                )
                dpg.bind_item_theme(run_btn, green_btn_theme)
    else:
        dpg.add_text("  Run Workflow: no dispatchable workflows", color=COL_DIM,
                     parent=parent)

    dpg.add_spacer(height=4, parent=parent)


def cb_switch_branch(sender, app_data, user_data):
    repo_key, combo_tag = user_data
    branch = dpg.get_value(combo_tag)
    if branch:
        executor.submit(bg_switch_branch, repo_key, branch)


def bg_switch_branch(repo_key, branch):
    rs = app.repos.get(repo_key)
    if not rs:
        return
    rc, stdout, stderr = run_git(["checkout", branch], cwd=str(rs.path))
    if rc == 0:
        ui_queue.put(("more_action_result", repo_key, True, f"Switched to {branch}"))
        bg_refresh_single_repo(repo_key)
    else:
        ui_queue.put(("more_action_result", repo_key, False,
                      f"Switch failed: {stderr.strip()}"))


def cb_remove_local_config(sender, app_data, user_data):
    executor.submit(bg_remove_local_config, user_data)


def bg_remove_local_config(repo_key):
    rs = app.repos.get(repo_key)
    if not rs:
        return
    cwd = str(rs.path)
    run_git(["config", "--local", "--unset", "user.name"], cwd=cwd)
    run_git(["config", "--local", "--unset", "user.email"], cwd=cwd)
    ui_queue.put(("more_action_result", repo_key, True, "Local config removed"))
    bg_refresh_single_repo(repo_key)


def cb_dispatch_workflow(sender, app_data, user_data):
    repo_key, wf_id, wf_name = user_data
    executor.submit(bg_dispatch_workflow, repo_key, wf_id, wf_name)


def bg_dispatch_workflow(repo_key, wf_id, wf_name):
    rs = app.repos.get(repo_key)
    if not rs:
        return
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["gh", "workflow", "run", str(wf_id)],
            cwd=str(rs.path),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=30, **kwargs,
        )
        if result.returncode == 0:
            ui_queue.put(("more_action_result", repo_key, True,
                          f"Dispatched '{wf_name}'"))
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
    return {k: {"path": ngf.path, "name": ngf.name} for k, ngf in app.non_git_folders.items()}


def rebuild_repos_ui(results, non_git_results=None, clear_errors=False):
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

        if new_entries or info.get("behind", 0) > 0:
            any_changes = True

        # Decide what to keep
        if name in preserved:
            prev_msg, prev_gen, prev_err = preserved[name]
            # Sticky errors survive rebuilds (cleared by manual Refresh)
            if prev_gen == GenStatus.ERROR and not clear_errors:
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
            git_user=info.get("git_user", ""),
            github_account=info.get("github_account", ""),
            visibility=info.get("visibility", ""),
            local_name=info.get("local_name", ""),
            local_email=info.get("local_email", ""),
            effective_name=info.get("effective_name", ""),
            effective_email=info.get("effective_email", ""),
            branch=info.get("branch", ""),
            last_commit_msg=info.get("last_commit_msg", ""),
            last_commit_date=info.get("last_commit_date", ""),
            ahead=info.get("ahead", 0),
            behind=info.get("behind", 0),
        )
        new_repos[name] = rs

    # Build non-git folder entries
    new_non_git = {}
    if non_git_results:
        for key, info in non_git_results.items():
            new_non_git[key] = NonGitFolder(path=info["path"], name=info["name"])

    # Compute max base-label width so dates right-align
    label_width = max((len(_repo_base_label(rs)) for rs in new_repos.values()), default=0)

    # Render git repos first (sorted), then non-git folders at the bottom
    for rs in sorted(new_repos.values(), key=lambda r: str(r.path).lower()):
        build_repo_section(rs, "repos_container", label_width=label_width)

    if app.show_non_git_folders:
        for ngf in sorted(new_non_git.values(), key=lambda n: str(n.path).lower()):
            build_non_git_section(ngf, "repos_container")

    app.repos = new_repos
    app.non_git_folders = new_non_git

    # Auto-generate for repos with changes and no message
    for name, rs in app.repos.items():
        if rs.entries and not rs.commit_message and rs.gen_status == GenStatus.IDLE:
            if app.auto_generate:
                rs.gen_status = GenStatus.GENERATING
                update_repo_status(rs)
                executor.submit(bg_generate_message, name)

    # Update tray alert based on whether any repos have changes
    if _window_hidden:
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
            if dpg.does_item_exist("gh_account_label"):
                dpg.set_value("gh_account_label", msg[1] if msg[1] else "")

        elif kind == "poll_result":
            results = msg[1]
            non_git = msg[2] if len(msg) > 2 else {}
            clear_errors = msg[3] if len(msg) > 3 else False
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
            if committed and pushed:
                rs.gen_status = GenStatus.IDLE
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                dpg.set_value(rs.status_tag, "Committed & pushed!")
                dpg.configure_item(rs.status_tag, color=COL_GREEN)
                executor.submit(bg_refresh_single_repo, repo_name)
                if app.actions_popup_enabled and rs.remote_url:
                    executor.submit(_launch_workflow_viewer, repo_name, rs)
            elif committed and not pushed:
                rs.gen_status = GenStatus.ERROR
                rs.commit_message = ""
                if rs.input_tag and dpg.does_item_exist(rs.input_tag):
                    dpg.set_value(rs.input_tag, "")
                rs.error_message = detail
                update_repo_status(rs)
            else:
                rs.gen_status = GenStatus.ERROR
                rs.error_message = detail
                update_repo_status(rs)

        elif kind == "workflow_check":
            _, repo_name, reason = msg
            rs = app.repos.get(repo_name)
            if rs and rs.status_tag and dpg.does_item_exist(rs.status_tag):
                if reason == "no_runs":
                    text = "Pushed — no Actions runs triggered for this commit"
                elif reason == "no_token":
                    text = "Pushed — Actions check skipped (no gh CLI token)"
                elif reason == "no_remote":
                    text = "Pushed — Actions check skipped (no remote/SHA)"
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
                # New repo being discovered — show placeholder
                dpg.add_text(
                    f"  {repo_display_name}  ...",
                    color=COL_DIM, parent="repos_container",
                )

        elif kind == "single_repo_refresh":
            _, repo_name, info = msg
            # Merge fresh data for this repo into current state and rebuild
            merged = {}
            for name, rs in app.repos.items():
                merged[name] = {
                    "path": rs.path,
                    "entries": rs.entries,
                    "remote_url": rs.remote_url,
                    "git_user": rs.git_user,
                    "github_account": rs.github_account,
                    "visibility": rs.visibility,
                    "local_name": rs.local_name,
                    "local_email": rs.local_email,
                    "effective_name": rs.effective_name,
                    "effective_email": rs.effective_email,
                    "branch": rs.branch,
                    "last_commit_msg": rs.last_commit_msg,
                    "last_commit_date": rs.last_commit_date,
                    "ahead": rs.ahead,
                    "behind": rs.behind,
                }
            merged[repo_name] = info
            rebuild_repos_ui(merged, _non_git_for_rebuild())

        elif kind == "refresh_then_generate":
            _, repo_name, info = msg
            # Merge fresh data and rebuild UI
            merged = {}
            for name, rs in app.repos.items():
                merged[name] = {
                    "path": rs.path,
                    "entries": rs.entries,
                    "remote_url": rs.remote_url,
                    "git_user": rs.git_user,
                    "github_account": rs.github_account,
                    "visibility": rs.visibility,
                    "local_name": rs.local_name,
                    "local_email": rs.local_email,
                    "effective_name": rs.effective_name,
                    "effective_email": rs.effective_email,
                    "branch": rs.branch,
                    "last_commit_msg": rs.last_commit_msg,
                    "last_commit_date": rs.last_commit_date,
                    "ahead": rs.ahead,
                    "behind": rs.behind,
                }
            merged[repo_name] = info
            rebuild_repos_ui(merged, _non_git_for_rebuild())
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
                        label=f"Incoming changes — {rs.name}",
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
                    executor.submit(bg_poll_repos)
                else:
                    dpg.set_value(rs.status_tag, f"Pull failed: {detail}")
                    dpg.configure_item(rs.status_tag, color=COL_RED)

        elif kind == "folder_selected":
            chosen = msg[1]
            folder = Path(chosen).resolve()
            if folder.is_dir() and folder not in app.watched_folders:
                app.watched_folders.append(folder)
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
    global green_btn_theme, link_btn_theme, remove_btn_theme, pull_btn_theme, _pending_topmost

    _acquire_instance_lock()
    args = parse_args()
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
        app.actions_popup_enabled = saved.get("actions_popup_enabled", True)
        app.show_non_git_folders = saved.get("show_non_git_folders", True)
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

        _gname, _gemail = get_git_global_user()
        app.global_git_name = _gname
        app.global_git_email = _gemail

        with dpg.group(horizontal=True):
            dpg.add_button(label="Add Folder", callback=cb_browse)
            dpg.add_button(label="Refresh", callback=cb_refresh)
            dpg.add_button(label="Pause", tag="pause_btn", callback=cb_pause)
            dpg.add_button(label="Settings", callback=cb_open_settings)
            dpg.add_spacer(width=10)
            dpg.add_text("", tag="gh_account_label", color=COL_GREEN)
            dpg.add_spacer(width=10)
            _global_label = f"{_gname} <{_gemail}>" if _gname and _gemail else _gname or _gemail or "not set"
            dpg.add_text(_global_label, color=COL_DIM)

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
        if not app.paused and now - app.last_poll >= app.poll_interval:
            trigger_poll()

        dpg.render_dearpygui_frame()

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
