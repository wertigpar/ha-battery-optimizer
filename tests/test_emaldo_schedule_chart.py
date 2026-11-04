"""Regression tests for the Emaldo internal-schedule chart sensor.

The coordinator splits the 192-slot continuous result into a today result and
a tomorrow result, truncating ``emaldo_modes`` to one day on each half
(``_split_result_by_day``). ``EmaldoScheduleChartSensor`` therefore cannot read
the 48 h window from ``last_result`` alone -- the second day lives in
``last_result_tomorrow``.
"""
import types

import pytest

from custom_components.battery_optimizer.const import SLOTS_PER_DAY
from custom_components.battery_optimizer.sensor import EmaldoScheduleChartSensor


def _coord(today_modes, tomorrow_modes):
    return types.SimpleNamespace(
        last_result=types.SimpleNamespace(emaldo_modes=list(today_modes), slots=[]),
        last_result_tomorrow=(
            types.SimpleNamespace(
                emaldo_modes=list(tomorrow_modes), slots=[]
            )
            if tomorrow_modes is not None
            else None
        ),
    )


def _sensor(coord):
    sensor = EmaldoScheduleChartSensor.__new__(EmaldoScheduleChartSensor)
    sensor.coordinator = coord
    return sensor


def test_today_only_when_backend_supplies_one_day():
    """Backend sent 96 slots -> chart emits exactly today, no fabrication."""
    sensor = _sensor(_coord([1] * SLOTS_PER_DAY, []))

    slots = sensor.extra_state_attributes["schedule"]

    assert len(slots) == SLOTS_PER_DAY
    assert {s["day"] for s in slots} == {0}


def test_tomorrow_emitted_when_backend_supplies_two_days():
    """Backend sent 192 slots -> chart must reach day 1 (the actual bug)."""
    sensor = _sensor(_coord([1] * SLOTS_PER_DAY, [-1] * SLOTS_PER_DAY))

    slots = sensor.extra_state_attributes["schedule"]

    assert len(slots) == SLOTS_PER_DAY * 2
    assert {s["day"] for s in slots} == {0, 1}
    assert slots[SLOTS_PER_DAY]["state"] == "Discharge"
    assert slots[SLOTS_PER_DAY]["slot"] == 0


def test_missing_tomorrow_result_is_tolerated():
    sensor = _sensor(_coord([0] * SLOTS_PER_DAY, None))

    slots = sensor.extra_state_attributes["schedule"]

    assert len(slots) == SLOTS_PER_DAY


def test_exposes_backend_mode_counts():
    """Counts make an upstream one-day payload observable from HA."""
    sensor = _sensor(_coord([1] * SLOTS_PER_DAY, [-1] * SLOTS_PER_DAY))

    attrs = sensor.extra_state_attributes

    assert attrs["emaldo_slots"] == SLOTS_PER_DAY * 2
    assert attrs["emaldo_slots_today"] == SLOTS_PER_DAY
    assert attrs["emaldo_slots_tomorrow"] == SLOTS_PER_DAY


def test_tomorrow_only_one_day_is_not_padded():
    """A partial tomorrow payload is emitted as-is, never padded to 96."""
    sensor = _sensor(_coord([1] * SLOTS_PER_DAY, [-1] * 10))

    attrs = sensor.extra_state_attributes

    assert attrs["emaldo_slots"] == SLOTS_PER_DAY + 10
    assert len(attrs["schedule"]) == SLOTS_PER_DAY + 10


def test_native_value_still_summarizes_today_only():
    """State string keeps its today-only contract (no dashboard churn)."""
    sensor = _sensor(_coord([1] * 3 + [-1] * 3, [1] * SLOTS_PER_DAY))

    assert sensor.native_value == "3C 3D 0I"


def test_no_schedule_when_absent():
    sensor = _sensor(_coord([], []))

    assert sensor.native_value == "no_schedule"
    assert sensor.extra_state_attributes == {}
