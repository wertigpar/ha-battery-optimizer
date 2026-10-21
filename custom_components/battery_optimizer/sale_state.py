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

The marker is only ever written for a sale that was OBSERVED running, so
RECOVER_COMPLETED means "the sale finished on its own" and not "the start was
refused".  A start Emaldo refuses does not raise, so a refused start would
otherwise leave a marker and consume the window with nothing exported.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

#: The switch reads off: the sale finished on its own.
#:
#: PRECONDITION: the marker is only ever written for a sale that was OBSERVED
#: running (the switch read back on).  Without that, a start that Emaldo
#: REFUSED - turn_on does not raise on a refusal - would leave a marker, and
#: this decision would consume the window as if it had been served, with
#: nothing exported.  That is the same money bug the refused-vs-finished
#: distinction in the coordinator exists to prevent.
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

    Every field the caller will use is validated, so "usable marker" is not a
    lie: the caller must be able to rebuild the ``(date, tuple)`` served key
    and the window end from the result without it raising.  A copy is
    returned, so a caller that mutates the result cannot mutate the marker
    it was given.

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
    # `type(s) is int`, not isinstance: bool is a subclass of int, and
    # tuple([True]) == (1,) - a bool-tainted window would match a real slot 1
    # and suppress a legitimate sale for the day.
    if not all(type(s) is int for s in data["served_window"]):
        return None
    # Every field the caller will use must be usable, or "usable marker" is a
    # lie and the caller crashes on a corrupt file.
    try:
        date.fromisoformat(data["served_date"])
    except (TypeError, ValueError):
        return None
    for key in ("started_at", "window_end"):
        try:
            datetime.fromisoformat(data[key])
        except (TypeError, ValueError):
            return None
    return dict(data)


def decide_sale_recovery(
    marker: dict[str, Any], now: datetime, switch_state: str | None
) -> str:
    """Decide what to do about a sale that was running when the process died.

    ``switch_state`` is "on", "off", or None/unknown when it cannot be read.
    The comparison against ``window_end`` is between absolute instants, so it
    holds across midnight.  The window is CLOSED at exactly ``window_end``
    (the coordinator's own rule is ``now_slot > window[-1]``, and
    ``window_end`` is the start of slot ``window[-1] + 1``), so the boundary
    is exclusive.

    Total: every path returns one of the four constants, never raises.
    """
    if switch_state == "off":
        return RECOVER_COMPLETED
    if switch_state != "on":
        return RECOVER_UNKNOWN
    if not isinstance(now, datetime):
        return RECOVER_UNKNOWN
    raw_end = marker.get("window_end")
    try:
        window_end = datetime.fromisoformat(raw_end)
    except (TypeError, ValueError):
        return RECOVER_UNKNOWN
    # Both sides are normalised to the same awareness, so the comparison can
    # never raise.  A naive `now` is treated as local wall-clock, which is
    # what the coordinator's own dt_util.now() would have produced.
    if now.tzinfo is None:
        window_end = window_end.replace(tzinfo=None)
    elif window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=now.tzinfo)
    return RECOVER_ADOPT if now < window_end else RECOVER_STOP


__all__ = [
    "RECOVER_ADOPT",
    "RECOVER_COMPLETED",
    "RECOVER_STOP",
    "RECOVER_UNKNOWN",
    "build_sale_marker",
    "decide_sale_recovery",
    "parse_sale_marker",
]
