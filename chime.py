"""Synthesize and play a melodic completion chime -- no external deps.

Renders a short bell-like WAV to %TEMP% on first use, then plays it via
the platform's native sound API. Two variants: success (ascending C major
arpeggio) and failure (descending minor third). Safe to call from any
thread; never raises.
"""

import math
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

SAMPLE_RATE = 22050
_cache = {}


def _render_notes(notes, total_seconds, sample_rate=SAMPLE_RATE):
    """Render (start_s, freq_hz, dur_s, amp) notes into 16-bit PCM bytes.

    Each note is a bell-like additive tone (fundamental + 2nd + 4th octave
    partials) with a 5ms linear attack and exponential decay over its
    duration. Notes overlap freely so arpeggios ring together.
    """
    n_samples = int(total_seconds * sample_rate)
    buf = [0.0] * n_samples
    partials = ((1.0, 1.0), (2.0, 0.5), (4.0, 0.25))
    attack_samples = int(0.005 * sample_rate)

    for start, freq, dur, amp in notes:
        i0 = int(start * sample_rate)
        i1 = min(i0 + int(dur * sample_rate), n_samples)
        tau = dur * 0.35
        two_pi_f = 2.0 * math.pi * freq
        for i in range(i0, i1):
            offset = i - i0
            t = offset / sample_rate
            if offset < attack_samples:
                env = offset / attack_samples
            else:
                env = math.exp(-t / tau)
            sample = (partials[0][1] * math.sin(two_pi_f * partials[0][0] * t)
                      + partials[1][1] * math.sin(two_pi_f * partials[1][0] * t)
                      + partials[2][1] * math.sin(two_pi_f * partials[2][0] * t))
            buf[i] += amp * env * sample

    peak = max((abs(x) for x in buf), default=1.0) or 1.0
    scale = 0.5 * 32767 / peak  # 6dB headroom
    return b"".join(
        struct.pack("<h", int(max(-32767, min(32767, x * scale))))
        for x in buf
    )


def _write_wav(path, pcm):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def _build_success():
    """C5 - E5 - G5 - C6, staggered 100ms apart, all ringing together."""
    C5, E5, G5, C6 = 523.25, 659.25, 783.99, 1046.50
    notes = [
        (0.00, C5, 0.55, 0.9),
        (0.10, E5, 0.55, 0.9),
        (0.20, G5, 0.55, 0.9),
        (0.30, C6, 0.65, 1.0),
    ]
    return _render_notes(notes, total_seconds=1.0)


def _build_failure():
    """E4 -> C4 descending minor third -- disappointed, not jarring."""
    E4, C4 = 329.63, 261.63
    notes = [
        (0.00, E4, 0.30, 0.9),
        (0.18, C4, 0.50, 0.9),
    ]
    return _render_notes(notes, total_seconds=0.75)


def _ensure_wav(name, builder):
    cached = _cache.get(name)
    if cached and os.path.isfile(cached):
        return cached
    path = Path(tempfile.gettempdir()) / f"ai-commit-chime-{name}.wav"
    if not path.is_file():
        _write_wav(path, builder())
    _cache[name] = str(path)
    return _cache[name]


def play(success=True):
    """Play the chime non-blocking. Swallows all errors."""
    try:
        path = _ensure_wav(
            "success" if success else "failure",
            _build_success if success else _build_failure,
        )
    except (OSError, ValueError):
        return

    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["afplay", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            for cmd in (["paplay", path], ["aplay", "-q", path]):
                try:
                    subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    return
                except FileNotFoundError:
                    continue
    except (OSError, ImportError):
        pass


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "success"
    play(success=(arg != "failure"))
    import time
    time.sleep(1.2)
