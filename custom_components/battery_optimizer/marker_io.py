"""Atomic JSON file I/O for restart markers.

Pure module (no Home Assistant imports, no relative imports) - unit-tested
directly (tests/test_marker_io.py).

A restart marker records something the process must not forget across a
restart.  It is written with the temp-file + fsync + os.replace dance, because
a plain open(path, "w") truncates in place: a power cut part way through the
write leaves a partial file, and a partial file reads back as corrupt - and
corrupt is explicitly NOT "nothing to restore".
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

#: Sentinel for "a file is there but cannot be read as a marker object".
#: Distinct from ``None`` ("no file at all") on purpose: a corrupt read must
#: never be mistaken for "nothing to restore".
MARKER_UNREADABLE = object()


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path``, atomically.

    The payload goes to a temp file in the same directory and is then
    os.replace()d onto the target, which is atomic on POSIX and on Windows.
    Raises ``OSError`` on failure.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=os.path.dirname(path) or ".",
            prefix=os.path.basename(path) + ".",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def read_json_marker(path: str) -> Any:
    """Read a JSON marker file.

    Returns the parsed object, ``None`` when there is no file at all, or
    ``MARKER_UNREADABLE`` when a file is there but is not a readable JSON
    object (truncated write, hand edit, wrong shape).  The last two must stay
    distinct.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return MARKER_UNREADABLE
    if not isinstance(data, dict):
        return MARKER_UNREADABLE
    return data


__all__ = ["MARKER_UNREADABLE", "atomic_write_json", "read_json_marker"]
