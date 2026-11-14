"""Battery Range (Emaldo slider) and contract-reserve helpers.

Pure module: no Home Assistant imports and no relative imports, so it is
unit-tested directly from tests/ (issue #42).

Two quantities live here because Guaranteed Grid Rewards contracts separate
them:

* the *planned SoC band* is a percentage of the whole pack (what the SoC
  sensor reports and what the optimizer plans with), narrowed by the
  capacity the contract reserves at the bottom and the top;
* the *Battery Range marker* is a percentage of the remaining usable
  capacity on such contracts, so it must be a separate user setting rather
  than a copy of ``soc_min``.
"""

from __future__ import annotations

#: Marker bounds.  0 is the idle slot byte (it stops every discharge) and 100
#: is not a valid Battery Range position.
MARKER_MIN = 1
MARKER_MAX = 99


def effective_soc_band(
    *,
    capacity_kwh: float,
    soc_min: float,
    soc_max: float,
    reserved_bottom_kwh: float = 0.0,
    reserved_top_kwh: float = 0.0,
) -> tuple[float, float]:
    """Return the planned SoC band after subtracting contract reserves.

    A Guaranteed Grid Rewards contract reserves part of the pack (the
    reporter's example: 6 kWh of 14.8 kWh, 3 kWh at the bottom and 3 kWh at
    the top).  Those kWh are not available to the optimizer, so they are
    removed from the band.  A Flexible contract reserves nothing and gets the
    band back unchanged.
    """
    soc_min = float(soc_min)
    soc_max = float(soc_max)
    capacity = float(capacity_kwh or 0.0)
    if capacity <= 0:
        return soc_min, soc_max
    bottom_pct = max(0.0, float(reserved_bottom_kwh or 0.0)) / capacity * 100.0
    top_pct = max(0.0, float(reserved_top_kwh or 0.0)) / capacity * 100.0
    return max(soc_min, bottom_pct), min(soc_max, 100.0 - top_pct)


def _bound_marker(value: float) -> int:
    """Round and clamp a marker into the valid Battery Range window."""
    return max(MARKER_MIN, min(MARKER_MAX, int(round(value))))


def resolve_low_marker(soc_min: float, configured: float | None) -> int:
    """Return the Battery Range low marker to push.

    ``configured`` is the optional ``battery_range_low_marker`` setting.  When
    it is unset the marker follows the planned floor, which is the historical
    behaviour and correct for Flexible contracts, where both scales are the
    same pack percentage.  A non-numeric stored value falls back to the
    planned floor instead of raising inside the push path.
    """
    planned = _bound_marker(float(soc_min))
    if configured is None:
        return planned
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return planned
    return _bound_marker(value)


def clamp_marker_to_high(marker: int, high: int | None) -> int:
    """Clamp the low marker so the pair stays valid.

    ha-emaldo requires ``high >= low`` (``valid_marker_pair``) and refuses a
    Battery Range write whose smart marker is below the emergency one, so a
    marker above the guard's high marker would otherwise be dropped silently.
    The marker is bounded into ``1..99`` first, and a high marker below
    ``MARKER_MIN`` (including the idle byte 0) is treated as "no high clamp",
    so this can never return a pair-breaking value.
    """
    marker = max(MARKER_MIN, min(MARKER_MAX, int(marker)))
    if high is None:
        return marker
    try:
        high_value = int(high)
    except (TypeError, ValueError):
        return marker
    if high_value < MARKER_MIN:
        return marker
    return min(marker, high_value)


def reserved_capacity_error(
    *,
    capacity_kwh: float,
    soc_min: float,
    soc_max: float,
    reserved_bottom_kwh: float,
    reserved_top_kwh: float,
) -> tuple[str, str] | None:
    """Return ``(side, error_key)`` when the reserved capacity is impossible.

    Only physically impossible input is rejected.  A reserve that merely
    narrows the band is accepted: reconciling the two scales is the user's
    call, and the effective band is shown in the options form.
    """
    capacity = float(capacity_kwh or 0.0)
    bottom = float(reserved_bottom_kwh or 0.0)
    top = float(reserved_top_kwh or 0.0)
    if bottom > capacity:
        return "bottom", "reserved_exceeds_capacity"
    if top > capacity:
        return "top", "reserved_exceeds_capacity"
    soc_min = float(soc_min)
    soc_max = float(soc_max)
    eff_min, eff_max = effective_soc_band(
        capacity_kwh=capacity,
        soc_min=soc_min,
        soc_max=soc_max,
        reserved_bottom_kwh=bottom,
        reserved_top_kwh=top,
    )
    if eff_min >= eff_max:
        if capacity > 0:
            bottom_pct = max(0.0, bottom) / capacity * 100.0
            top_pct = max(0.0, top) / capacity * 100.0
        else:
            bottom_pct = top_pct = 0.0
        if eff_min == bottom_pct and bottom_pct > soc_min:
            return "bottom", "invalid_reserved_band"
        if eff_max == 100.0 - top_pct and eff_max < soc_max:
            return "top", "invalid_reserved_band"
        return "base", "invalid_soc_band"
    return None


__all__ = [
    "MARKER_MAX",
    "MARKER_MIN",
    "clamp_marker_to_high",
    "effective_soc_band",
    "reserved_capacity_error",
    "resolve_low_marker",
]
