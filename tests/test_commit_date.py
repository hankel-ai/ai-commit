"""Tests for ai_commit_core.parse_commit_date -- the pure ``%ci`` -> local-time
conversion behind the ``[Jul 19 03:51pm]`` stamp in each repo header.

``git log --format=%ci`` prints the timestamp in the *committer's* timezone with
an explicit offset (``2026-07-20 21:30:00 +0530``). The offset must be honoured
and the result rendered in the *viewer's* local zone, otherwise a commit pushed
from another timezone (a GitLab colleague in IST, a CI runner on UTC) displays as
a wrong -- often future -- local time.

Every assertion here is timezone-independent: expectations are derived from the
running machine's own zone rather than hardcoded, so the suite passes in EDT, IST
or UTC alike.

Run: python tests/test_commit_date.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_commit_core as core

_failures = []


def check(name, cond):
    if cond:
        print(f"ok  {name}")
    else:
        print(f"FAIL {name}")
        _failures.append(name)


def _expected_stamp(ts):
    """How the app should render epoch *ts* -- formatted in the local zone."""
    return (datetime.fromtimestamp(ts)
            .strftime("%b %d %I:%M%p")
            .replace("AM", "am").replace("PM", "pm"))


IST = timezone(timedelta(hours=5, minutes=30))
EDT = timezone(timedelta(hours=-4))
UTC = timezone.utc

# One single instant, written three ways by three different committers.
INSTANT = datetime(2026, 7, 20, 16, 0, 0, tzinfo=UTC)
AS_IST = "2026-07-20 21:30:00 +0530"
AS_EDT = "2026-07-20 12:00:00 -0400"
AS_UTC = "2026-07-20 16:00:00 +0000"


# --- the offset must be honoured, not discarded ----------------------------

def test_offset_is_applied_to_timestamp():
    # The epoch value is absolute: all three spellings are the same moment.
    for label, s in (("ist", AS_IST), ("edt", AS_EDT), ("utc", AS_UTC)):
        _, ts = core.parse_commit_date(s)
        check(f"epoch_{label}", ts == INSTANT.timestamp())


def test_same_instant_renders_identically():
    # The regression: dropping the offset made these three disagree by hours.
    stamps = {core.parse_commit_date(s)[0] for s in (AS_IST, AS_EDT, AS_UTC)}
    check("same_instant_one_stamp", len(stamps) == 1)


def test_rendered_in_viewer_local_zone():
    for label, s in (("ist", AS_IST), ("edt", AS_EDT), ("utc", AS_UTC)):
        stamp, ts = core.parse_commit_date(s)
        check(f"local_render_{label}", stamp == _expected_stamp(ts))


def test_ist_commit_is_not_in_the_future():
    # A colleague in IST commits "now"; ai-commit must not show a future time.
    now = datetime.now(IST)
    _, ts = core.parse_commit_date(now.strftime("%Y-%m-%d %H:%M:%S %z"))
    check("ist_not_future", ts <= datetime.now(timezone.utc).timestamp() + 1)


# --- formatting details preserved from the original implementation ---------

def test_am_pm_lowercased():
    stamp, _ = core.parse_commit_date("2026-07-20 09:05:00 +0000")
    check("lowercase_meridiem",
          stamp.endswith("am") or stamp.endswith("pm"))
    check("no_upper_meridiem", "AM" not in stamp and "PM" not in stamp)


def test_midnight_and_noon():
    # %I renders midnight as 12am and noon as 12pm, in local terms.
    for label, s in (("midnight", "2026-07-20 00:00:00 +0000"),
                     ("noon", "2026-07-20 12:00:00 +0000")):
        stamp, ts = core.parse_commit_date(s)
        check(f"{label}_matches_local", stamp == _expected_stamp(ts))


# --- degenerate input ------------------------------------------------------

def test_empty_input():
    check("empty", core.parse_commit_date("") == ("", 0.0))
    check("whitespace", core.parse_commit_date("   ") == ("", 0.0))


def test_missing_offset_treated_as_local():
    # Defensive: a %ci without an offset should still render, assumed local.
    stamp, ts = core.parse_commit_date("2026-07-20 12:00:00")
    check("no_offset_renders", stamp == _expected_stamp(ts))
    check("no_offset_has_ts", ts > 0)


def test_garbage_falls_back_without_raising():
    stamp, ts = core.parse_commit_date("not a date at all")
    check("garbage_no_crash", isinstance(stamp, str) and ts == 0.0)


if __name__ == "__main__":
    for fn in list(globals().values()):
        if callable(fn) and getattr(fn, "__name__", "").startswith("test_"):
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        sys.exit(1)
    print("all passed")
