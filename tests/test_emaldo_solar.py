"""Tests for `emaldo_solar.py` — the Emaldo-native solar forecast reader.

The Emaldo `schedule_chart` sensor publishes its own PV forecast as the
per-slot ``solar`` field (watts) of a rolling 15-minute window whose ``t``
values are ISO timestamps *with offsets*.  These tests pin the two conversions
that are easy to get wrong and expensive to get wrong in production:

* W -> kW, and the 15-minute slot index (``hour * 4 + minute // 15``).
* Date attribution through the *local* timezone, not by string slicing — a slot
  written as ``2026-01-01T23:30:00+00:00`` is already the next local day at
  +01:00 and must land in that day's array.

Plus the "unknown data" contract: anything the unit does not report stays 0.0,
never ``None``, so the planner never sees a hole.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from . import load_emaldo_solar

emaldo_solar, const = load_emaldo_solar()
SLOTS_PER_DAY = const.SLOTS_PER_DAY

# Fixed +01:00: fixtures write "+01:00" timestamps and read the wall clock back
# as local time, with no IANA database dependency.
LOCAL_TZ = timezone(timedelta(hours=1))
DAY = date(2026, 1, 1)


def _schedule(day=DAY, watts=None, first_slot=0, count=SLOTS_PER_DAY):
    """Slots ``first_slot .. first_slot+count-1`` of one *local* day, watts.

    ``watts`` is either a number or a callable taking the slot index.
    """
    base = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
    out = []
    for index in range(first_slot, first_slot + count):
        value = 0.0 if watts is None else (watts(index) if callable(watts) else watts)
        stamp = (base + timedelta(minutes=index * 15)).isoformat()
        out.append({"t": stamp, "solar": value})
    return out


class TestSlotMapping(unittest.TestCase):
    def test_full_day_converts_watts_to_kw_at_slot_indices(self):
        """A 96-slot day maps to 96 kW values, one per 15 minutes."""
        values = [0.0] * SLOTS_PER_DAY
        values[0] = 100.0  # 00:00 local
        values[40] = 4000.0  # 10:00 local
        slots = emaldo_solar.emaldo_solar_day(
            _schedule(watts=lambda i: values[i]), DAY, LOCAL_TZ
        )
        self.assertEqual(len(slots), SLOTS_PER_DAY)
        self.assertAlmostEqual(slots[0], 0.1, places=9)
        self.assertAlmostEqual(slots[40], 4.0, places=9)
        self.assertEqual(sum(1 for v in slots if v > 0), 2)

    def test_off_quarter_minute_lands_in_its_quarter(self):
        """10:07 local is slot 40, the same quarter as 10:00."""
        schedule = [{"t": "2026-01-01T10:07:00+01:00", "solar": 2000.0}]
        slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        self.assertAlmostEqual(slots[40], 2.0, places=9)
        self.assertEqual(slots[41], 0.0)

    def test_day_boundary_slots_stay_on_their_own_day(self):
        schedule = [
            {"t": "2026-01-01T00:00:00+01:00", "solar": 500.0},
            {"t": "2026-01-01T23:45:00+01:00", "solar": 700.0},
            {"t": "2026-01-02T00:00:00+01:00", "solar": 900.0},
        ]
        today = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        tomorrow = emaldo_solar.emaldo_solar_day(schedule, date(2026, 1, 2), LOCAL_TZ)
        self.assertAlmostEqual(today[0], 0.5, places=9)
        self.assertAlmostEqual(today[SLOTS_PER_DAY - 1], 0.7, places=9)
        self.assertEqual(sum(today) , 1.2)
        self.assertAlmostEqual(tomorrow[0], 0.9, places=9)

    def test_local_timezone_decides_the_day_not_the_timestamp_prefix(self):
        """23:30Z is 00:30 local (+01:00): it belongs to the NEXT local day.

        Slicing the ISO string, or grouping on the raw date part, would put this
        slot in Jan 1 — the planner would then plan a night slot as pre-dawn PV
        and double-count the following morning.
        """
        schedule = [{"t": "2026-01-01T23:30:00+00:00", "solar": 3000.0}]
        jan1 = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        jan2 = emaldo_solar.emaldo_solar_day(schedule, date(2026, 1, 2), LOCAL_TZ)
        self.assertEqual(sum(jan1), 0.0)
        self.assertAlmostEqual(jan2[2], 3.0, places=9)  # 00:30 local

    def test_rolling_window_partial_day_is_padded_with_zeros(self):
        """A schedule covering 12 slots leaves the other 84 at 0.0."""
        slots = emaldo_solar.emaldo_solar_day(
            _schedule(watts=1000.0, first_slot=36, count=12), DAY, LOCAL_TZ
        )
        self.assertEqual(len(slots), SLOTS_PER_DAY)
        self.assertEqual(sum(1 for v in slots if v > 0), 12)
        self.assertAlmostEqual(slots[36], 1.0, places=9)
        self.assertAlmostEqual(slots[47], 1.0, places=9)
        self.assertEqual(slots[35], 0.0)
        self.assertEqual(slots[48], 0.0)

    def test_other_days_in_a_multi_day_window_are_ignored(self):
        """Two days of schedule -> exactly the requested day comes back."""
        schedule = _schedule(DAY, 1000.0) + _schedule(date(2026, 1, 2), 5000.0)
        slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        self.assertAlmostEqual(sum(slots), SLOTS_PER_DAY * 1.0, places=6)


class TestUnknownData(unittest.TestCase):
    def test_missing_or_empty_schedule_is_all_zeros_not_none(self):
        for schedule in (None, [], "nonsense", {}):
            slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
            self.assertEqual(len(slots), SLOTS_PER_DAY)
            self.assertEqual(sum(slots), 0.0)

    def test_non_numeric_or_non_positive_solar_stays_zero(self):
        junk = [None, "", "1200", True, False, -5.0, 0, [], {}]
        schedule = [
            {"t": f"2026-01-01T{index:02d}:00:00+01:00", "solar": value}
            for index, value in enumerate(junk)
        ]
        slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        self.assertEqual(sum(slots), 0.0)

    def test_implausible_power_is_dropped(self):
        """A field in the wrong unit must not be planned as 120 kW of PV."""
        schedule = [
            {"t": "2026-01-01T12:00:00+01:00", "solar": 120_000.0},  # rejected
            {"t": "2026-01-01T12:15:00+01:00", "solar": 30_000.0},  # 30 kW kept
        ]
        slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        self.assertAlmostEqual(slots[48], 0.0, places=9)
        self.assertAlmostEqual(slots[49], 30.0, places=9)

    def test_malformed_entries_are_skipped_without_raising(self):
        schedule = [
            None,
            7,
            "slot",
            {"solar": 1000.0},  # no timestamp
            {"t": "not-a-time", "solar": 1000.0},
            {"t": 12345, "solar": 1000.0},
            {"t": "2026-01-01T12:00:00+01:00", "solar": 1500.0},
        ]
        slots = emaldo_solar.emaldo_solar_day(schedule, DAY, LOCAL_TZ)
        self.assertEqual(len(slots), SLOTS_PER_DAY)
        self.assertAlmostEqual(slots[48], 1.5, places=9)
        self.assertEqual(sum(slots), 1.5)


if __name__ == "__main__":
    unittest.main()
