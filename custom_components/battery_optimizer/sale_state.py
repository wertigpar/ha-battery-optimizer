"""Forced-sell restart marker: payload shape and the recovery decision.

Pure module (no Home Assistant imports, no relative imports) - unit-tested
directly (tests/test_sale_state.py).

A restart loses the in-memory ``_manual_selling_active`` flag, and every stop
path then treats the *Manual selling* switch as not ours, so no ``turn_off``
is ever sent and the sale keeps exporting to the grid until Emaldo's own
staged target happens to run out.

The marker records that a sale was running, for WHICH window, and until WHEN.
The window identity is needed because ``_manual_selling_served`` is what stops
a second sale opening for the same window, and the normal completion path
cannot restore it after a restart (it returns early when the sale is not
active).  The timestamps are absolute, so the marker is still read - and still
means something - across midnight.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: The switch reads off: the sale finished on its own.
RECOVER_COMPLETED = "completed"
#: The switch reads on and the window is still live: re-adopt the sale.
RECOVER_ADOPT = "adopt"
#: The switch reads on and the window has closed: stop it.
RECOVER_STOP = "stop"
#: The switch state could not be read: cannot tell, so do not guess.
RECOVER_UNKNOWN = "unknown"

_REQUIRED_KEYS = ("started_at", "window_end", "served_date", "served_window")


def build_sale_marker(
    now: datetime, window: list[int], window_end: datetime
) -> dict[str, Any]:
    """Build the marker payload for a sale that is starting.

    ``window`` is the plan's day-relative slot list; ``window_end`` is the
    absolute instant the window closes.
    """
    return {
        "started_at": now.isoformat(),
        "window_end": window_end.isoformat(),
        "served_date": now.date().isoformat(),
        "served_window": list(window),
    }


def parse_sale_marker(data: Any) -> dict[str, Any] | None:
    """Normalise a marker payload, or return None when the shape is wrong.

    None means "this is not a usable marker" - the caller must treat that the
    same way it treats an unreadable file, NOT as "nothing to restore".
    """
    if not isinstance(data, dict):
        return None
    for key in _REQUIRED_KEYS:
        if key not in data:
            return None
    if not isinstance(data["served_window"], list):
        return None
    if not all(isinstance(s, int) for s in data["served_window"]):
        return None
    return data


def decide_sale_recovery(
    marker: dict[str, Any], now: datetime, switch_state: str | None
) -> str:
    """Decide what to do about a sale that was running when the process died.

    ``switch_state`` is "on", "off", or None/unknown when it cannot be read.
    The comparison against ``window_end`` is between absolute instants, so it
    holds across midnight.
    """
    if switch_state == "off":
        return RECOVER_COMPLETED
    if switch_state != "on":
        return RECOVER_UNKNOWN
    try:
        window_end = datetime.fromisoformat(marker["window_end"])
    except (TypeError, ValueError):
        return RECOVER_UNKNOWN
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=now.tzinfo)
    return RECOVER_ADOPT if now <= window_end else RECOVER_STOP


__all__ = [
    "RECOVER_ADOPT",
    "RECOVER_COMPLETED",
    "RECOVER_STOP",
    "RECOVER_UNKNOWN",
    "build_sale_marker",
    "decide_sale_recovery",
    "parse_sale_marker",
]
