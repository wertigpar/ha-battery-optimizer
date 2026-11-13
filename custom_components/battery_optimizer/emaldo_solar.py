"""Emaldo-native solar forecast — schedule_chart ``solar`` field → planner slots.

Pure module (no Home Assistant imports): stateless, deterministic,
unit-testable.

Semantics
---------
The Emaldo unit publishes its *own* PV forecast as the per-slot ``solar`` field
of the ``sensor.<device>_schedule_chart`` attribute ``schedule`` — a flat list
of 15-minute dicts::

    {"t": "2026-11-13T10:45:00+01:00", "price": 130.03, "solar": 16, ...}

``solar`` is AC-side power in **watts**.  The planner wants 96 × 15-minute
**kW** values for one local day, so this module maps each slot onto its
15-minute index inside the requested day (``hour * 4 + minute // 15``) and
converts W → kW.

Two things the caller must handle, and does (see
``Coordinator._get_emaldo_solar_forecast``):

* Not every Emaldo model/config supplies the field — for a Store unit with
  third-party PV off the backend may report ``solar`` as always zero.  A day
  that parses to all zeros is indistinguishable from a clear night, so the
  caller checks it once and warns instead of silently planning with no PV.
* The schedule is a rolling window: a day can be partially covered (e.g. right
  after midnight before the unit refreshes).  Missing slots stay 0.0 — the same
  "unknown solar" treatment the Solcast path uses when a sensor is absent.

Unlike Solcast there is exactly one estimate here, so there is no p10/p50
dispersion; callers fall back to the median path and
``compute_solar_confidence_ratio`` correctly reports "no confidence data".
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo

from .const import SLOTS_PER_DAY

# A forecast slot is only trusted when it is a plausible PV power: the field is
# watts, so a value of several MW means the unit is reporting something else and
# must not be fed to the planner as kW.
_MAX_PLAUSIBLE_W = 50_000.0


def _is_number(value) -> bool:
    """True for real numbers; booleans are ints in Python and must NOT pass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def emaldo_solar_day(
    schedule: list | None,
    day: date,
    local_tz: tzinfo,
) -> list[float]:
    """Return ``SLOTS_PER_DAY`` x 15-min kW values for one local day.

    Args:
        schedule: The raw ``schedule`` attribute (list of dicts with ``t`` and
            ``solar``), or ``None``.
        day: Local calendar date to extract.
        local_tz: HA's configured local timezone, used to date-shift the
            ISO timestamps (the schedule is a rolling window with offsets).

    Returns:
        96 floats in kW. Slots the schedule does not cover, or whose ``solar``
        value is missing/non-numeric, are 0.0 — never ``None``.
    """
    slots = [0.0] * SLOTS_PER_DAY
    if not schedule:
        return slots
    for entry in schedule:
        if not isinstance(entry, dict):
            continue
        raw_t = entry.get("t")
        if not isinstance(raw_t, str):
            continue
        try:
            slot_dt = datetime.fromisoformat(raw_t).astimezone(local_tz)
        except ValueError:
            continue
        if slot_dt.date() != day:
            continue
        index = slot_dt.hour * 4 + slot_dt.minute // 15
        if not 0 <= index < SLOTS_PER_DAY:
            continue
        watts = entry.get("solar")
        if not _is_number(watts) or watts <= 0.0 or watts > _MAX_PLAUSIBLE_W:
            continue
        slots[index] = float(watts) / 1000.0
    return slots
