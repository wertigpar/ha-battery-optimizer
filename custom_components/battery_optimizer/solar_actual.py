"""Pure helpers for the external actual-solar sensor.

No Home Assistant imports — unit-tested directly (tests/test_solar_actual.py).
"""

from __future__ import annotations


def normalize_to_kwh(value: float, unit: str | None) -> float | None:
    """Normalize a cumulative energy counter reading to kWh.

    Returns None when the value is not numeric or the unit is neither ``Wh``
    nor ``kWh`` (the caller treats None as "skip this record").
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if unit is None:
        return None
    u = unit.strip().lower()
    if u == "wh":
        return value / 1000.0
    if u == "kwh":
        return value
    return None


def counter_delta_kwh(snap: float, current: float) -> tuple[float, bool]:
    """Delta of a daily cumulative counter since the snapshot.

    Returns ``(delta_kwh, reset_crossed)``. A drop larger than 0.1 kWh means
    the daily counter reset at midnight: the pre-reset fraction is
    unmeasurable, so the post-reset accumulation is returned best-effort with
    ``reset_crossed=True`` (such records are excluded from the auto-tune).
    """
    delta = current - snap
    if delta >= -0.1:
        return max(delta, 0.0), False
    return max(current, 0.0), True


def resolve_solar_source(
    configured: bool, snapshot_available: bool, current_available: bool
) -> str:
    """Pick the actual-solar source for this accuracy record.

    ``"external"`` — counter configured and readable on both sides: diff it.
    ``"emaldo"``   — not configured: use the Emaldo-internal estimate.
    ``"skip"``     — configured but the counter is unavailable on one side:
                     drop the record so the auto-tune never mixes sources.
    """
    if not configured:
        return "emaldo"
    if snapshot_available and current_available:
        return "external"
    return "skip"


__all__ = ["normalize_to_kwh", "counter_delta_kwh", "resolve_solar_source"]
