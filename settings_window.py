#!/usr/bin/env python3
"""ai-commit Settings -- a separate OS window, run as its own process.

Dear PyGui gives one OS window (viewport) per process, so Settings runs here,
launched by ai-commit-gui ("Settings" button), and talks to it over pipes:

  main -> stdin   JSON lines. First line ``{"type": "init", "values": {...},
                  "status": {...}, "anchor": {x, y, w, h}, ...}``; later
                  ``{"type": "values"}`` (a setting changed elsewhere, e.g. the
                  toolbar) and ``{"type": "status"}`` (status lines).
  stdout -> main  ``{"type": "set", "key": k, "value": v}`` per edit, and one
                  ``{"type": "ready", ...}`` with the fitted size.

The main process stays the only writer of the settings file and the only
place settings take effect; this window just edits and displays. EOF on stdin
(main exited or closed us) closes the window.

The window sizes itself to its content (capped at the monitor's work area)
and re-fits when rows are added or removed.
"""

import json
import os
import queue
import sys
import threading
from pathlib import Path

import dearpygui.dearpygui as dpg

from ai_commit_core import remote_host

COL_BG = (30, 30, 35)
COL_ACCENT = (100, 140, 230)
COL_RED = (220, 80, 80)
COL_DIM = (120, 120, 130)
COL_WHITE = (220, 220, 225)

WINDOW_TITLE = "ai-commit - Settings"
WINDOW_WIDTH = 1000
WRAP = 450  # text wrap width inside one of the two columns
GAP = 8            # between this window and the main window / screen edge

IDENTITY_FIELDS = (("host", "Host", "gitlab.example.com"),
                   ("name", "Name", "exactly as on the other PC"),
                   ("email", "Email", "exactly as on the other PC"),
                   ("login", "Login", "account username (optional)"))


def _set_always_top(value):
    """Follow the main window's always-on-top, so Settings is never under it."""
    try:
        dpg.set_viewport_always_top(bool(value))
    except Exception:
        pass  # no viewport (render tests)


def normalize_host(value):
    value = str(value or "").strip()
    if "://" in value or "@" in value:
        value = remote_host(value)
    return value.lower()


class SettingsWindow:
    """The Settings UI. ``send`` receives every outgoing message dict."""

    def __init__(self, init, send):
        self.init = init or {}
        self.values = dict(self.init.get("values") or {})
        self.limits = dict(self.init.get("limits") or {})
        self.send = send
        self.pins = [dict(p) for p in self.values.get("identity_pins") or []]
        self.proxy_url = ""
        self.refit = True
        self.ready_sent = False
        self._pin_handler = None

    # -- outgoing ------------------------------------------------------------

    def _set(self, key, value):
        self.values[key] = value
        if key == "always_on_top":
            _set_always_top(value)
        self.send({"type": "set", "key": key, "value": value})

    def _cb_value(self, sender, app_data, user_data):
        """Generic widget callback; *user_data* is the setting key."""
        value = app_data
        if isinstance(value, str):
            value = value.strip()
            if user_data == "kiro_cli_path":
                value = value.strip('"').strip()
        self._set(user_data, value)

    def send_pins(self, *_args):
        self._set("identity_pins", [dict(p) for p in self.pins])

    def _cb_pin_field(self, sender, app_data, user_data):
        index, key = user_data
        if 0 <= index < len(self.pins):
            self.pins[index][key] = (normalize_host(app_data) if key == "host"
                                     else str(app_data or "").strip())

    def _cb_pin_add(self, *_args):
        self.pins.append({"host": "", "name": "", "email": "", "login": ""})
        self._build_pin_rows()

    def _cb_pin_remove(self, sender, app_data, user_data):
        if 0 <= user_data < len(self.pins):
            del self.pins[user_data]
        self._build_pin_rows()
        self.send_pins()

    def _cb_copy_url(self, *_args):
        if self.proxy_url:
            dpg.set_clipboard_text(self.proxy_url)

    def _cb_test_chime(self, sender, app_data, user_data):
        try:
            import chime
            chime.play(success=bool(user_data))
        except Exception:
            pass

    def _cb_close(self, *_args):
        dpg.stop_dearpygui()

    # -- incoming ------------------------------------------------------------

    def handle(self, msg):
        kind = msg.get("type")
        if kind == "values":
            for key, value in (msg.get("values") or {}).items():
                self.values[key] = value
                if key == "identity_pins":
                    self.pins = [dict(p) for p in value or []]
                    self._build_pin_rows()
                elif dpg.does_item_exist(f"s_{key}"):
                    dpg.set_value(f"s_{key}", value)
                if key == "always_on_top":
                    _set_always_top(value)
        elif kind == "status":
            self._apply_status(msg.get("status") or {})
        elif kind == "eof":
            dpg.stop_dearpygui()

    def _apply_status(self, status):
        if "kiro" in status and dpg.does_item_exist("s_kiro_status"):
            kiro = status["kiro"] or {}
            dpg.set_value("s_kiro_status", kiro.get("text", ""))
            dpg.configure_item("s_kiro_status",
                               color=COL_RED if kiro.get("error") else COL_DIM)
        if "git_proxy" in status and dpg.does_item_exist("s_git_proxy_status"):
            proxy = status["git_proxy"] or {}
            self.proxy_url = proxy.get("url", "")
            dpg.set_value("s_git_proxy_status", proxy.get("text", ""))
        if "identity" in status and dpg.does_item_exist("s_identity_status"):
            ident = status["identity"] or {}
            dpg.set_value("s_identity_status", "\n".join(ident.get("lines") or []))
            dpg.configure_item("s_identity_status",
                               color=COL_DIM if ident.get("ok", True) else COL_RED)
        if "global_identity" in status and dpg.does_item_exist("s_global_identity"):
            who = status["global_identity"] or "no global user.email set"
            dpg.set_value("s_global_identity",
                          f"Other hosts: warn unless {who} (global)")
        self.refit = True

    # -- building ------------------------------------------------------------

    def _int(self, label, key, lo, hi, suffix="", width=90):
        with dpg.group(horizontal=True):
            dpg.add_text(label, color=COL_DIM)
            dpg.add_input_int(tag=f"s_{key}", default_value=int(self.values.get(key, lo)),
                              width=width, min_value=lo, min_clamped=True,
                              max_value=hi, max_clamped=True, step=0,
                              callback=self._cb_value, user_data=key)
            if suffix:
                dpg.add_text(suffix, color=COL_DIM)

    def _check(self, label, key):
        dpg.add_checkbox(label=label, tag=f"s_{key}",
                         default_value=bool(self.values.get(key, False)),
                         callback=self._cb_value, user_data=key)

    def _text(self, label, key, hint="", password=False):
        with dpg.group(horizontal=True):
            dpg.add_text(label, color=COL_DIM)
            dpg.add_input_text(tag=f"s_{key}", default_value=str(self.values.get(key, "")),
                               width=-1, hint=hint, password=password,
                               callback=self._cb_value, user_data=key)

    def _heading(self, text):
        dpg.add_spacer(height=6)
        dpg.add_text(text, color=COL_ACCENT)
        dpg.add_separator()

    def _build_pin_rows(self):
        if not dpg.does_item_exist("s_identity_rows"):
            return
        dpg.delete_item("s_identity_rows", children_only=True)
        for i, pin in enumerate(self.pins):
            with dpg.group(parent="s_identity_rows"):
                for key, label, hint in IDENTITY_FIELDS:
                    with dpg.group(horizontal=True):
                        dpg.add_text(f"{label}:", color=COL_DIM)
                        dpg.add_spacer(width=max(0, 48 - 7 * len(label)))
                        tag = f"s_pin_{i}_{key}"
                        dpg.add_input_text(tag=tag, default_value=pin.get(key, ""),
                                           width=-90 if key == "host" else -1,
                                           hint=hint, callback=self._cb_pin_field,
                                           user_data=(i, key))
                        dpg.bind_item_handler_registry(tag, self._pin_handler)
                        if key == "host":
                            dpg.add_button(label="Remove", width=-1,
                                           callback=self._cb_pin_remove, user_data=i)
                dpg.add_spacer(height=4)
        self.refit = True

    def _build_general(self):
        dpg.add_text("Polling", color=COL_ACCENT)
        dpg.add_separator()
        self._int("Poll interval:", "poll_interval", 5, 600, "s")
        self._int("Poll threads: ", "poll_threads", 1,
                  int(self.limits.get("poll_threads_max", 16)))
        dpg.add_text("8 is the practical max on Windows -- process spawn, "
                     "not bandwidth, is the limit", color=COL_DIM, wrap=WRAP)
        self._int("Idle repo poll:", "idle_poll_interval", 60, 86400, "s")

        self._heading("Behavior")
        self._check("Auto-generate commit messages", "auto_generate")
        self._check("Always on top", "always_on_top")
        self._check("Actions popup after push", "actions_popup_enabled")
        with dpg.group(horizontal=True):
            self._check("Chime when workflow completes", "chime_on_completion")
            dpg.add_button(label="Test OK", callback=self._cb_test_chime,
                           user_data=True)
            dpg.add_button(label="Test Fail", callback=self._cb_test_chime,
                           user_data=False)
        if self.init.get("startup_supported"):
            self._check("Run at startup", "run_at_startup")

        self._heading("Provider")
        self._text("Base URL:", "ollama_url", "http://localhost:11434")
        self._text("API key: ", "api_key",
                   "litellm only; blank = AI_COMMIT_API_KEY", password=True)
        self._text("Kiro CLI:", "kiro_cli_path", "Blank = find automatically")
        dpg.add_text("", tag="s_kiro_status", color=COL_DIM, wrap=WRAP)

        self._heading("Display")
        self._check("Show non-git folders", "show_non_git_folders")
        self._check("Recent repos only", "recent_only")
        self._int("Recent window:", "recent_days", 1, 3650, "days")

    def _build_identity(self):
        dpg.add_text("Commit Identity", color=COL_ACCENT)
        dpg.add_separator()
        self._check("Check identity before commit/push", "identity_guard_enabled")
        dpg.add_text("Pinned host: commits use this identity (the app writes "
                     "it into git config) and a different identity BLOCKS "
                     "the commit/push. Login sets the account git signs in "
                     "with for that host.", color=COL_DIM, wrap=WRAP)
        dpg.add_spacer(height=2)
        dpg.add_group(tag="s_identity_rows")
        dpg.add_button(label="+ Add host", callback=self._cb_pin_add)
        dpg.add_text("", tag="s_identity_status", color=COL_DIM, wrap=WRAP)
        dpg.add_text("", tag="s_global_identity", color=COL_DIM, wrap=WRAP)

    def _build_git_proxy(self):
        self._heading("Git Proxy (LAN)")
        self._check("Serve watched repos over LAN (read-only)",
                    "git_proxy_enabled")
        with dpg.group(horizontal=True):
            dpg.add_text("Port:", color=COL_DIM)
            dpg.add_input_int(tag="s_git_proxy_port",
                              default_value=int(self.values.get("git_proxy_port", 0)),
                              width=90, min_value=1024, min_clamped=True,
                              max_value=65535, max_clamped=True, step=0,
                              callback=self._cb_value, user_data="git_proxy_port")
            dpg.add_button(label="Copy URL", callback=self._cb_copy_url)
        dpg.add_text("", tag="s_git_proxy_status", color=COL_DIM, wrap=WRAP)
        dpg.add_text("Others can 'git clone <url>/<repo>.git'. Fetch/pull only "
                     "-- pushing is refused. No authentication: LAN clients only.",
                     color=COL_DIM, wrap=WRAP)

    def build(self):
        """Create every item inside window ``primary``. Needs a DPG context."""
        with dpg.item_handler_registry() as self._pin_handler:
            dpg.add_item_deactivated_after_edit_handler(callback=self.send_pins)

        with dpg.window(tag="primary", no_title_bar=True, no_move=True,
                        no_close=True, no_collapse=True):
            with dpg.group(tag="s_content"):
                # Two columns: one column of everything is taller than a
                # 1080p screen, and the point is to see every setting at once.
                with dpg.table(header_row=False, borders_innerV=True,
                               policy=dpg.mvTable_SizingStretchSame,
                               pad_outerX=False):
                    dpg.add_table_column()
                    dpg.add_table_column()
                    with dpg.table_row():
                        with dpg.group():
                            self._build_general()
                        with dpg.group():
                            self._build_identity()
                            self._build_git_proxy()
                dpg.add_spacer(height=6)
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Close", width=90, callback=self._cb_close)
                    dpg.add_text("Changes apply immediately.", color=COL_DIM)
        self._build_pin_rows()
        self._apply_status(self.init.get("status") or {})

    # -- sizing ----------------------------------------------------------------

    def content_height(self):
        return int(dpg.get_item_rect_size("s_content")[1])

    def fit(self, max_height):
        """Resize the viewport so the content fits. True once measured."""
        content = self.content_height()
        if content <= 0:
            return False
        chrome = dpg.get_viewport_height() - dpg.get_viewport_client_height()
        want = min(content + 2 * 6 + 12 + chrome, max_height)
        if abs(dpg.get_viewport_height() - want) > 2:
            dpg.set_viewport_height(want)
        return True


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------

def _work_area(x, y):
    """(left, top, right, bottom) of the monitor holding point (x, y)."""
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                            ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.restype = wt.HMONITOR
            user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
            mon = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)  # NEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(mon, ctypes.byref(info)):
                r = info.rcWork
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, 1920, 1040


def _place(anchor, width, height, area):
    """Left of the main window if it fits there, else right; clamped on screen."""
    left, top, right, bottom = area
    ax, ay = int(anchor.get("x", left)), int(anchor.get("y", top))
    aw = int(anchor.get("w", 0))
    x = ax - width - GAP
    if x < left:
        x = ax + aw + GAP
    if x + width > right:
        x = max(left, right - width)
    y = max(top, min(ay, bottom - height))
    return x, y


def _dark_title_bar():
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes as wt
        user32 = ctypes.windll.user32
        pid = os.getpid()
        found = []
        proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(hwnd, _l):
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
            return True
        user32.EnumWindows(proto(cb), 0)
        value = ctypes.c_int(1)
        for hwnd in found:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


def _theme():
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, COL_BG)
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (50, 50, 60))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (60, 60, 75))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (70, 70, 85))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 65, 85))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (75, 80, 105))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, COL_ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Text, COL_WHITE)
            dpg.add_theme_color(dpg.mvThemeCol_CheckMark, COL_ACCENT)
            dpg.add_theme_color(dpg.mvThemeCol_Separator, (55, 55, 65))
            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 6, 4)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 6, 4)
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 3)
            dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10, 6)
    dpg.bind_theme(theme)


def run(init, stdin, stdout):
    inbox = queue.Queue()
    out_lock = threading.Lock()
    broken = threading.Event()

    def send(msg):
        if broken.is_set():
            return
        try:
            with out_lock:
                stdout.write(json.dumps(msg) + "\n")
                stdout.flush()
        except (OSError, ValueError):
            broken.set()

    def reader():
        for line in stdin:
            line = line.strip()
            if line:
                try:
                    inbox.put(json.loads(line))
                except ValueError:
                    pass
        inbox.put({"type": "eof"})

    threading.Thread(target=reader, daemon=True).start()

    anchor = init.get("anchor") or {}
    area = _work_area(anchor.get("x", 0) + anchor.get("w", 0) // 2,
                      anchor.get("y", 0) + 20)
    max_height = area[3] - area[1] - 2 * GAP

    dpg.create_context()
    _theme()
    vp = dict(title=WINDOW_TITLE, width=WINDOW_WIDTH, height=max_height,
              min_width=420, min_height=200,
              always_on_top=bool(init.get("values", {}).get("always_on_top")))
    icon = Path(__file__).resolve().parent / "ai-commit-icon.ico"
    if icon.is_file():
        vp["small_icon"] = vp["large_icon"] = str(icon)
    x, y = _place(anchor, WINDOW_WIDTH, max_height, area)
    dpg.create_viewport(x_pos=x, y_pos=y, **vp)

    win = SettingsWindow(init, send)
    win.build()
    dpg.set_primary_window("primary", True)
    dpg.setup_dearpygui()
    dpg.show_viewport()

    frames = 0
    while dpg.is_dearpygui_running():
        while True:
            try:
                win.handle(inbox.get_nowait())
            except queue.Empty:
                break
        if broken.is_set():
            break
        dpg.render_dearpygui_frame()
        frames += 1
        if frames == 3:
            _dark_title_bar()
        if win.refit and frames >= 2 and win.fit(max_height):
            win.refit = False
            h = dpg.get_viewport_height()
            nx, ny = _place(anchor, dpg.get_viewport_width(), h, area)
            if not win.ready_sent:
                dpg.set_viewport_pos([nx, ny])
            elif dpg.get_viewport_pos()[1] + h > area[3]:
                dpg.set_viewport_pos([dpg.get_viewport_pos()[0], max(area[1], area[3] - h)])
            if not win.ready_sent:
                win.ready_sent = True
                send({"type": "ready", "width": dpg.get_viewport_width(),
                      "height": h, "client_height": dpg.get_viewport_client_height(),
                      "content_height": win.content_height(),
                      "max_height": max_height})
    dpg.destroy_context()


def main():
    first = sys.stdin.readline()
    try:
        init = json.loads(first) if first.strip() else {}
    except ValueError:
        init = {}
    run(init, sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
