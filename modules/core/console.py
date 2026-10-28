"""
modules/console.py
------------------
Make stdout/stderr safe for the log text this project actually writes.

The pipeline logs freely with typographic characters — em dashes in section
headers, arrows in "point->cell CSR", the superscript two in "W/m2", box
characters in the banner.  On Windows, when stdout is a *console* Python talks
to the console API directly and all of that works; but as soon as stdout is
redirected to a pipe or a file, Python falls back to the ANSI code page
(cp1252 here), and printing any of those characters raises
UnicodeEncodeError.

That is not a display glitch.  Every worker logs through a helper that does
both ``print(msg)`` and ``log_fn(msg)``, so the exception propagates out of the
print and kills the worker mid-run — a crash caused purely by the decoration in
a log string.  It bites exactly when output is being captured, i.e. when a run
is being logged for the record.

Fixing it at the stream rather than by policing every string literal is
deliberate: there are ~30 such literals across the logging paths today, the
GUI label text legitimately uses the same characters, and any future log line
would reintroduce the bug.  One call at each process entry point covers all of
it permanently.
"""

from __future__ import annotations

import sys


def configure_stdio() -> None:
    """Make sys.stdout/sys.stderr encode any text without raising.

    Safe to call more than once, and on interpreters where the streams are
    absent (pythonw) or already owned by a host that wrapped them.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # pythonw gives None streams; some hosts replace them with plain
            # objects that have no reconfigure().  Nothing to do either way.
            continue
        try:
            # UTF-8 keeps the characters intact in a redirected log file.
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError, AttributeError):
            try:
                # Stream refuses a new encoding (already detached, for
                # instance).  Settle for never raising: unencodable characters
                # become "?" instead of killing the worker.
                reconfigure(errors="replace")
            except (OSError, ValueError, AttributeError):
                pass
