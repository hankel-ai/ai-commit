"""Tray icon drawing and the rotating sweep shown while a refresh is running.

Pillow is imported inside the functions that draw, so importing this module
costs nothing and works before the GUI's dependency check has run.
"""
import threading
import time

ICON_BLUE = (100, 140, 230, 255)
ICON_SIZE = 64

FRAME_COUNT = 12
FRAME_INTERVAL = 0.1    # seconds; one full turn takes about 1.2 s
MIN_SPIN = 1.2          # seconds; one full turn, so a very short refresh is still visible

_SUPERSAMPLE = 4
_WEDGE_COLOR = (18, 32, 92)
_WEDGE_SPAN = 250       # degrees of fading tail behind the leading edge
_WEDGE_SLICES = 50


def draw_icon_background(draw):
    """Rounded blue square."""
    draw.rounded_rectangle([2, 2, 62, 62], radius=12, fill=ICON_BLUE)


def draw_icon_glyph(draw):
    """Git branch: vertical line, two commit dots, one side branch."""
    draw.line([(32, 14), (32, 50)], fill=(255, 255, 255, 220), width=3)
    draw.ellipse([25, 12, 39, 26], fill=(255, 255, 255, 240))  # top commit
    draw.ellipse([25, 38, 39, 52], fill=(255, 255, 255, 240))  # bottom commit
    # Inner dots (the commit "holes")
    draw.ellipse([29, 16, 35, 22], fill=ICON_BLUE)
    draw.ellipse([29, 42, 35, 48], fill=ICON_BLUE)
    # Side branch line
    draw.line([(32, 20), (44, 30), (44, 38), (38, 44)], fill=(255, 255, 255, 200), width=2)


def build_sweep_frames(count=FRAME_COUNT):
    """Return *count* icon images with a dark wedge turning clockwise behind the glyph.

    The wedge sits under the glyph so the branch stays readable, and it is dark so
    the white glyph keeps its contrast wherever the wedge is.
    """
    from PIL import Image, ImageDraw

    big = ICON_SIZE * _SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [2 * _SUPERSAMPLE, 2 * _SUPERSAMPLE, 62 * _SUPERSAMPLE, 62 * _SUPERSAMPLE],
        radius=12 * _SUPERSAMPLE, fill=255)
    empty = Image.new("L", (big, big), 0)

    background = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw_icon_background(ImageDraw.Draw(background))
    glyph = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw_icon_glyph(ImageDraw.Draw(glyph))

    # Oversized so the slices' outer edge clears the square's corners.
    box = [-20 * _SUPERSAMPLE, -20 * _SUPERSAMPLE, 84 * _SUPERSAMPLE, 84 * _SUPERSAMPLE]
    slice_span = _WEDGE_SPAN / _WEDGE_SLICES
    frames = []
    for index in range(count):
        wedge = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(wedge)
        head = 360 * index / count - 90     # Pillow's 0 degrees is 3 o'clock
        # Tail first: ImageDraw overwrites, so the darkest slice must land last.
        for step in range(_WEDGE_SLICES, -1, -1):
            fade = step / _WEDGE_SLICES
            start = head - fade * _WEDGE_SPAN
            draw.pieslice(box, start, start + slice_span + 1,
                          fill=_WEDGE_COLOR + (int(235 * (1 - fade) ** 1.2),))
        wedge.putalpha(Image.composite(wedge.getchannel("A"), empty, mask))

        frame = background.copy()
        frame.alpha_composite(wedge.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS))
        frame.alpha_composite(glyph)
        frames.append(frame)
    return frames


class RefreshActivity:
    """Counts refreshes in progress. Safe to call from any thread."""

    def __init__(self, min_spin=MIN_SPIN):
        self._min_spin = min_spin
        self._lock = threading.Lock()
        self._count = 0
        self._spin_until = 0.0
        self.wake = threading.Event()

    def begin(self):
        with self._lock:
            if self._count == 0:
                self._spin_until = time.monotonic() + self._min_spin
            self._count += 1
        self.wake.set()

    def end(self):
        with self._lock:
            self._count -= 1

    @property
    def active(self):
        with self._lock:
            return self._count > 0

    def is_spinning(self):
        with self._lock:
            return self._count > 0 or time.monotonic() < self._spin_until


class TrayAnimator:
    """Owns every write to the tray icon once started.

    pystray's icon setter releases the old icon handle and loads a new one, so two
    threads writing at once could race. The main thread only calls set_alert().
    A frame write costs about 10 ms, so this thread blocks when there is no refresh.
    """

    def __init__(self, icon, activity, idle_image, add_alert, title, alert_title):
        self._icon = icon
        self._activity = activity
        self._idle = idle_image
        self._add_alert = add_alert
        self._title = title
        self._alert_title = alert_title
        self._alert = False
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def set_alert(self, on):
        if on != self._alert:
            self._alert = on
            self._activity.wake.set()

    def stop(self):
        self._stopping.set()
        self._activity.wake.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)

    def _write(self, image, title):
        # Writes fail while Explorer restarts. The next write or poll recovers,
        # and an exception here would end the animation for the rest of the session.
        try:
            self._icon.title = title
            self._icon.icon = image
        except Exception:
            pass

    def _run(self):
        # Built here, not in start(), so the GUI's startup does not wait for it.
        frames = build_sweep_frames()
        alert_frames = [self._add_alert(f) for f in frames]
        idle_alert = self._add_alert(self._idle)
        index = 0
        shown_idle = None
        while not self._stopping.is_set():
            # Clear before reading state: a later begin() or set_alert() wakes the wait.
            self._activity.wake.clear()
            alert = self._alert
            title = self._alert_title if alert else self._title
            if self._activity.is_spinning():
                self._write((alert_frames if alert else frames)[index % len(frames)], title)
                index += 1
                shown_idle = None
                self._stopping.wait(FRAME_INTERVAL)
            else:
                if shown_idle is not alert:
                    self._write(idle_alert if alert else self._idle, title)
                    shown_idle = alert
                index = 0
                if not self._stopping.is_set():
                    self._activity.wake.wait()
