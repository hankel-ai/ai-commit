"""The tray icon shows a rotating sweep only while a refresh is really running.

Uses real Pillow frames and, on Windows, a real pystray icon in the real tray, so
a small icon appears in the notification area for a few seconds during the run.

Run: python tests/test_tray_animation.py
"""
import importlib.util
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Neutralize the GUI's startup self-detach so exec_module just defines symbols.
os.environ["_AI_COMMIT_GUI_CHILD"] = "1"

from PIL import Image

import tray_animation as ta

_spec = importlib.util.spec_from_file_location("acg", os.path.join(ROOT, "ai-commit-gui.py"))
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

_failures = []

BLUE = (100, 140, 230)
ORANGE_CENTRE = (255, 180, 40, 255)   # middle of the changes dot, at (51, 13)


def check(name, cond):
    print(("ok  " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def wait_until(pred, timeout=4.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def is_dark(px):
    """Clearly darker than the icon's plain blue background."""
    return px[2] < BLUE[2] - 40


# --- frames ---------------------------------------------------------------

def test_wedge_rotates_clockwise():
    frames = ta.build_sweep_frames(12)
    check("twelve frames", len(frames) == 12)
    # Frame 0 has the wedge's leading edge at 12 o'clock, trailing counter-clockwise.
    # (44, 10) is just clockwise of 12 o'clock, so it is ahead of the edge.
    # (20, 10) is just counter-clockwise, so it is under the darkest part.
    check("frame 0: ahead of the edge stays plain blue", frames[0].getpixel((44, 10))[:3] == BLUE)
    check("frame 0: behind the edge is dark", is_dark(frames[0].getpixel((20, 10))))
    # Frame 3 is a quarter turn later: edge at 3 o'clock, so (44, 10) is now behind it.
    check("frame 3: the wedge has moved clockwise", is_dark(frames[3].getpixel((44, 10))))


def test_branch_glyph_stays_on_top():
    for i, frame in enumerate(ta.build_sweep_frames(12)):
        if frame.getpixel((32, 32))[0] < 200:
            check(f"frame {i}: branch line stays bright", False)
            return
    check("branch line stays bright in every frame", True)


def test_wedge_stays_inside_rounded_square():
    for i, frame in enumerate(ta.build_sweep_frames(12)):
        if frame.getpixel((0, 0))[3] != 0 or frame.getpixel((63, 63))[3] != 0:
            check(f"frame {i}: corners stay transparent", False)
            return
    check("corners stay transparent in every frame", True)


# --- activity -------------------------------------------------------------

def test_overlapping_refreshes_spin_until_the_last_ends():
    act = ta.RefreshActivity(min_spin=0)
    check("starts not spinning", not act.is_spinning())
    act.begin()
    act.begin()
    act.end()
    check("one of two finished: still spinning", act.is_spinning())
    act.end()
    check("both finished: stops", not act.is_spinning())


def test_very_short_refresh_still_spins_for_min_spin():
    act = ta.RefreshActivity(min_spin=0.15)
    act.begin()
    act.end()
    check("short refresh: spinning right after it ended", act.is_spinning())
    time.sleep(0.2)
    check("short refresh: stops after min_spin", not act.is_spinning())


def test_long_refresh_outlasts_min_spin():
    act = ta.RefreshActivity(min_spin=0.05)
    act.begin()
    time.sleep(0.1)
    check("still running past min_spin: spinning", act.is_spinning())
    act.end()
    check("ended past min_spin: stops at once", not act.is_spinning())


# --- refresh entry points -------------------------------------------------

def _tracked(name, invoke, stub_name):
    """While the function runs activity is on, and an exception must not leave it on."""
    seen = {}

    def boom(*a, **kw):
        seen["active"] = m._refresh_activity.active
        raise RuntimeError("simulated failure")

    original = getattr(m, stub_name)
    setattr(m, stub_name, boom)
    try:
        invoke()
    except RuntimeError:
        pass
    finally:
        setattr(m, stub_name, original)
    check(f"{name}: activity on while it runs", seen.get("active") is True)
    check(f"{name}: activity off after it raises", m._refresh_activity.active is False)


def test_refresh_functions_report_activity():
    with tempfile.TemporaryDirectory() as tmp:
        m.app.repos["repo"] = SimpleNamespace(
            path=Path(tmp), remote_url="", visibility="")
        try:
            _tracked("bg_poll_repos", lambda: m.bg_poll_repos(),
                     "get_active_github_account")
            _tracked("bg_refresh_single_repo", lambda: m.bg_refresh_single_repo("repo"),
                     "get_last_commit")
            _tracked("bg_refresh_then_generate", lambda: m.bg_refresh_then_generate("repo"),
                     "get_last_commit")
        finally:
            m.app.repos.pop("repo", None)


# --- animator in the real tray --------------------------------------------

def _real_icon(idle):
    """A real pystray icon that also counts real icon writes."""
    import pystray
    import threading

    class CountingIcon(pystray.Icon):
        writes = 0
        fail_next = False

        def _update_icon(self):
            type(self).writes += 1
            if type(self).fail_next:
                type(self).fail_next = False
                raise OSError("simulated Shell_NotifyIcon failure")
            super()._update_icon()

    icon = CountingIcon("ai_commit_tray_test", idle, "tray test")
    threading.Thread(target=icon.run, daemon=True).start()
    assert wait_until(lambda: icon.visible), "tray icon never became visible"
    return icon


def _animator(icon, idle, activity):
    a = ta.TrayAnimator(icon, activity, idle, m._make_alert_icon, "normal", "changes")
    a.start()
    return a


def _distinct_frames(icon, seconds):
    seen = set()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        seen.add(id(icon.icon))
        time.sleep(0.01)
    return len(seen)


def test_spins_only_while_active():
    idle = Image.new("RGBA", (64, 64), BLUE + (255,))
    act = ta.RefreshActivity(min_spin=0)
    icon = _real_icon(idle)
    a = _animator(icon, idle, act)
    try:
        check("idle icon showing before any refresh", wait_until(lambda: icon.icon is idle))
        act.begin()
        check("refresh started: icon leaves idle", wait_until(lambda: icon.icon is not idle))
        check("refresh running: frames advance", _distinct_frames(icon, 0.6) >= 3)
        act.end()
        check("refresh ended: back to the idle image", wait_until(lambda: icon.icon is idle))
        settled = wait_until(lambda: not act.is_spinning()) and type(icon).writes
        time.sleep(0.4)
        check("idle: no icon writes at all", type(icon).writes == settled)
    finally:
        a.stop()
        icon.stop()


def test_changes_dot_shows_idle_and_spinning():
    idle = Image.new("RGBA", (64, 64), BLUE + (255,))
    act = ta.RefreshActivity(min_spin=0)
    icon = _real_icon(idle)
    a = _animator(icon, idle, act)
    try:
        a.set_alert(True)
        check("idle + changes: dot shows",
              wait_until(lambda: icon.icon.getpixel((51, 13)) == ORANGE_CENTRE))
        check("idle + changes: tooltip says so", wait_until(lambda: icon.title == "changes"))
        act.begin()
        wait_until(lambda: icon.icon.getpixel((20, 10))[:3] != BLUE)
        check("spinning + changes: dot still shows",
              icon.icon.getpixel((51, 13)) == ORANGE_CENTRE)
        a.set_alert(False)
        check("spinning, changes cleared: dot goes",
              wait_until(lambda: icon.icon.getpixel((51, 13)) != ORANGE_CENTRE))
        act.end()
        check("finished: plain idle image", wait_until(lambda: icon.icon is idle))
        check("finished: tooltip normal", icon.title == "normal")
    finally:
        a.stop()
        icon.stop()


def test_failed_icon_write_does_not_kill_animation():
    idle = Image.new("RGBA", (64, 64), BLUE + (255,))
    act = ta.RefreshActivity(min_spin=0)
    icon = _real_icon(idle)
    a = _animator(icon, idle, act)
    try:
        wait_until(lambda: type(icon).writes > 0)
        type(icon).fail_next = True
        act.begin()
        check("the simulated failure fired", wait_until(lambda: not type(icon).fail_next))
        check("frames still advance after a failed write", _distinct_frames(icon, 0.6) >= 3)
    finally:
        act.end()
        a.stop()
        icon.stop()


def test_stop_ends_icon_writes():
    idle = Image.new("RGBA", (64, 64), BLUE + (255,))
    act = ta.RefreshActivity(min_spin=0)
    icon = _real_icon(idle)
    a = _animator(icon, idle, act)
    act.begin()
    wait_until(lambda: icon.icon is not idle)
    a.stop()
    frozen = type(icon).writes
    time.sleep(0.3)
    check("stopped mid-spin: no more writes", type(icon).writes == frozen)
    a.stop()   # a second stop (menu Quit, then main-loop cleanup) must be harmless
    icon.stop()


def test_app_tray_wiring():
    """Real setup_tray: alert goes through the animator and Quit stops it."""
    m._icon_image = Image.new("RGBA", (64, 64), BLUE + (255,))
    m.setup_tray()
    icon = m.tray_icon
    check("tray came up", wait_until(lambda: icon.visible))
    m._set_tray_alert(True)
    check("_set_tray_alert: dot shows",
          wait_until(lambda: icon.icon.getpixel((51, 13)) == ORANGE_CENTRE))
    m._set_tray_alert(False)
    check("_set_tray_alert off: dot goes",
          wait_until(lambda: icon.icon.getpixel((51, 13)) != ORANGE_CENTRE))
    m._refresh_activity.begin()
    try:
        check("refresh activity animates the app's icon", _distinct_frames(icon, 0.8) >= 3)
    finally:
        m._refresh_activity.end()
    quit_item = [i for i in icon.menu.items if i.text == "Quit"][0]
    quit_item(icon)
    animator = m._tray_animator
    frozen = id(icon.icon)
    m._refresh_activity.begin()
    time.sleep(0.3)
    m._refresh_activity.end()
    check("Quit stops the animator", id(icon.icon) == frozen and not animator._thread.is_alive())


def main():
    tests = [
        test_wedge_rotates_clockwise,
        test_branch_glyph_stays_on_top,
        test_wedge_stays_inside_rounded_square,
        test_overlapping_refreshes_spin_until_the_last_ends,
        test_very_short_refresh_still_spins_for_min_spin,
        test_long_refresh_outlasts_min_spin,
        test_refresh_functions_report_activity,
    ]
    if sys.platform == "win32":
        tests += [
            test_spins_only_while_active,
            test_changes_dot_shows_idle_and_spinning,
            test_failed_icon_write_does_not_kill_animation,
            test_stop_ends_icon_writes,
            test_app_tray_wiring,
        ]
    else:
        print("skip real-tray tests: Windows only")
    for t in tests:
        print(f"--- {t.__name__}")
        try:
            t()
        except Exception:
            traceback.print_exc()
            check(f"{t.__name__} raised", False)
    print(f"\n{len(_failures)} failure(s)")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
