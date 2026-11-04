"""Forced-sell restart marker: payload shape and recovery decision.

Pure-module tests (sale_state.py): no HA imports, no relative imports.

The decision table is the whole fix.  A restart loses the in-memory
"_manual_selling_active" flag, and every stop path then treats the switch as
not ours, so no turn_off is ever sent and the sale runs on unmanaged until
Emaldo's own staged target runs out.
"""

from __future__ import annotations

import datetime as _dt
from datetime import datetime, timedelta, timezone

from . import load_sale_state

sale_state, const = load_sale_state()
TZ = timezone(timedelta(hours=3))


def _now(hour=18, minute=20, day=20):
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def _marker(window=(84, 85), started=None, window_end=None):
    now = started or _now()
    if window_end is None:
        window_end = datetime(2026, 10, 20, 0, 0, tzinfo=TZ) + timedelta(
            minutes=15 * (window[-1] + 1)
        )
    return sale_state.build_sale_marker(now, list(window), window_end)


def test_build_marker_carries_the_window_identity():
    m = _marker()
    assert m["served_date"] == "2026-10-20"
    assert m["served_window"] == [84, 85]
    assert m["started_at"].startswith("2026-10-20T18:20")
    assert m["window_end"].startswith("2026-10-20T21:30")


def test_build_marker_window_end_can_cross_midnight():
    """A window whose last slot is 95 ends at midnight the next day."""
    m = _marker(window=(94, 95), started=_now(hour=23, minute=50))
    assert m["window_end"].startswith("2026-10-21T00:00")


def test_parse_accepts_a_built_marker():
    m = _marker()
    parsed = sale_state.parse_sale_marker(m)
    assert parsed is not None
    assert parsed["served_window"] == [84, 85]


def test_parse_rejects_wrong_shapes():
    assert sale_state.parse_sale_marker(None) is None
    assert sale_state.parse_sale_marker({"we_enabled": True}) is None
    assert sale_state.parse_sale_marker({"served_date": "x"}) is None


def test_decide_completed_when_the_switch_reads_off():
    assert (
        sale_state.decide_sale_recovery(_marker(), _now(), "off")
        == sale_state.RECOVER_COMPLETED
    )


def test_decide_adopt_while_inside_the_window():
    assert (
        sale_state.decide_sale_recovery(_marker(), _now(), "on")
        == sale_state.RECOVER_ADOPT
    )


def test_decide_stop_once_past_the_window():
    assert (
        sale_state.decide_sale_recovery(_marker(), _now(hour=21, minute=45), "on")
        == sale_state.RECOVER_STOP
    )


def test_decide_unknown_when_the_switch_state_cannot_be_read():
    assert (
        sale_state.decide_sale_recovery(_marker(), _now(), None)
        == sale_state.RECOVER_UNKNOWN
    )
    assert (
        sale_state.decide_sale_recovery(_marker(), _now(), "unavailable")
        == sale_state.RECOVER_UNKNOWN
    )


def test_decision_holds_across_midnight():
    """A sale started at 23:50 with a window ending at 00:00 next day.

    The marker is still read on the next calendar day - a day-relative rule
    would discard it and leave the switch on.
    """
    m = _marker(window=(94, 95), started=_now(hour=23, minute=50, day=20))
    # 00:10 on the 21st: past the window -> stop.
    assert (
        sale_state.decide_sale_recovery(m, _now(hour=0, minute=10, day=21), "on")
        == sale_state.RECOVER_STOP
    )
    # 23:55 on the 20th: still inside -> adopt.
    assert (
        sale_state.decide_sale_recovery(m, _now(hour=23, minute=55, day=20), "on")
        == sale_state.RECOVER_ADOPT
    )


def test_decision_uses_absolute_instants_not_calendar_days():
    """The same wall-clock minute on two different days decides differently."""
    m = _marker(window=(84, 85))
    # 18:20 on the 20th is inside; 18:20 on the 21st is also inside, but the
    # window_end is absolute, so a much later time is not.
    assert (
        sale_state.decide_sale_recovery(m, _now(hour=18, minute=20, day=20), "on")
        == sale_state.RECOVER_ADOPT
    )
    assert (
        sale_state.decide_sale_recovery(m, _now(hour=23, minute=59, day=20), "on")
        == sale_state.RECOVER_STOP
    )


def test_parse_rejects_a_non_list_window():
    m = _marker()
    m["served_window"] = "84"
    assert sale_state.parse_sale_marker(m) is None


def test_parse_rejects_a_non_int_slot():
    m = _marker()
    m["served_window"] = [84, "85"]
    assert sale_state.parse_sale_marker(m) is None


def test_parse_rejects_a_bool_slot():
    m = _marker()
    m["served_window"] = [True, False]
    assert sale_state.parse_sale_marker(m) is None


def test_parse_rejects_an_unparseable_served_date():
    m = _marker()
    m["served_date"] = "not-a-date"
    assert sale_state.parse_sale_marker(m) is None


def test_parse_rejects_an_unparseable_window_end():
    m = _marker()
    m["window_end"] = "not-a-datetime"
    assert sale_state.parse_sale_marker(m) is None


def test_decide_is_unknown_for_a_bad_window_end():
    m = _marker()
    m["window_end"] = "not-a-datetime"
    assert (
        sale_state.decide_sale_recovery(m, _now(), "on")
        == sale_state.RECOVER_UNKNOWN
    )


def test_decide_is_unknown_for_a_bad_window_end_even_when_the_switch_reads_off():
    """The marker is validated FIRST.

    A payload nobody could parse must not consume a window on the strength
    of the switch state: "completed" is a decision about the window, and the
    window is exactly what could not be read.
    """
    m = _marker()
    m["window_end"] = "not-a-datetime"
    assert (
        sale_state.decide_sale_recovery(m, _now(), "off")
        == sale_state.RECOVER_UNKNOWN
    )


def test_decide_is_unknown_for_an_unparsed_marker():
    assert (
        sale_state.decide_sale_recovery({"served_date": "2026-10-20"}, _now(), "on")
        == sale_state.RECOVER_UNKNOWN
    )


def test_decide_does_not_raise_for_a_naive_now():
    naive = _dt.datetime(2026, 10, 20, 18, 20)
    assert (
        sale_state.decide_sale_recovery(_marker(), naive, "on")
        == sale_state.RECOVER_ADOPT
    )


def test_window_end_boundary_is_closed():
    """At exactly window_end the coordinator considers the window closed."""
    m = _marker()
    end = _dt.datetime.fromisoformat(m["window_end"])
    assert (
        sale_state.decide_sale_recovery(m, end, "on") == sale_state.RECOVER_STOP
    )
    assert (
        sale_state.decide_sale_recovery(m, end - _dt.timedelta(microseconds=1), "on")
        == sale_state.RECOVER_ADOPT
    )


def test_decide_is_unknown_for_a_non_dict_marker():
    assert (
        sale_state.decide_sale_recovery(None, _now(), "on")
        == sale_state.RECOVER_UNKNOWN
    )
    assert (
        sale_state.decide_sale_recovery([], _now(), "on")
        == sale_state.RECOVER_UNKNOWN
    )


def test_parse_returns_a_copy_the_caller_cannot_mutate():
    m = _marker()
    parsed = sale_state.parse_sale_marker(m)
    parsed["served_window"].append(999)
    parsed["served_date"] = "1999-01-01"
    assert m["served_window"] == [84, 85], "the source window list was mutated"
    assert m["served_date"] == "2026-10-20", "the source dict was mutated"
