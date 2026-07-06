"""Append-only debug log so a user can send us an exact traceback when the builder fails.

IMPORTANT: this only captures builder/Python errors -- failing to build or install a pak,
missing files, bad state, etc. It does NOT (and cannot) capture an in-game crash: the game
is a separate process that runs long after the builder has finished and exited. If the pak
builds fine but the game crashes mid-play, this log will look clean -- that's expected.

The log lives in the temp dir (always writable, unlike Program Files), and its path is
printed at startup and appended to any error shown in the UI so it's easy to find and share.
"""

import os
import sys
import time
import tempfile
import traceback

PATH = os.path.join(tempfile.gettempdir(), "bwamod-debug.log")


def _append(text):
    try:
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:  # noqa: BLE001 - logging must never itself break the tool
        pass


def note(text):
    """Append a timestamped line (no traceback) -- e.g. what was built."""
    _append(f"[{time.strftime('%H:%M:%S')}] {text}\n")


def record(context):
    """Append a timestamped line plus the currently-active exception traceback, if any."""
    tb = traceback.format_exc()
    if tb.strip() == "NoneType: None":
        tb = ""
    _append(f"[{time.strftime('%H:%M:%S')}] {context}\n{tb}{chr(10) if tb else ''}")


def install(context):
    """Mark the start of a run and route otherwise-uncaught exceptions into the log.
    Returns the log path so callers can show it to the user."""
    _append(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {context} =====\n")
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        _append("".join(traceback.format_exception(exc_type, exc, tb)))
        prev(exc_type, exc, tb)

    sys.excepthook = hook
    return PATH
