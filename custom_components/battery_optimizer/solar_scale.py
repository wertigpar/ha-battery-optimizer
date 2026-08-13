"""Solar forecast scale — over-forecast compensation auto-tune.

Pure module (no Home Assistant imports): stateless, deterministic, unit-testable.

Semantics
---------
The planner scales the solar forecast by a whole-day multiplier
``solar_forecast_scale`` (default 0.0 = auto). When set to 0.0 the scale is
tuned per run from the accuracy sidecar: each historical run's
``(actual - planned)`` error is converted to a *raw-basis* ratio
``(actual / planned_scaled) * scale_used``, which equals the true unscaled
bias regardless of the scale the plan actually ran under. An EWMA over those
ratios (seed 1.0) produces the next scale, clipped to [SOLAR_SCALE_MIN,
SOLAR_SCALE_MAX].

The raw-basis normalization matters: a plan that ran at scale 0.5 and was
exactly accurate (``planned_scaled == actual``) has a naive scaled-basis
ratio of 1.0 — the system would see a "perfect" forecast and never correct
the underlying bias. The raw-basis ratio correctly reports 0.5.
"""

from __future__ import annotations

from .const import (
    SOLAR_SCALE_MAX,
    SOLAR_SCALE_MIN,
    SOLAR_SCALE_EWMA_ALPHA,
    SOLAR_SCALE_WARMUP_RECORDS,
    SOLAR_SCALE_MIN_ELAPSED_SLOTS,
    SOLAR_SCALE_MIN_PLANNED_KWH,
)

_AUTO_SENTINEL = 0.0


def _is_number(value) -> bool:
    """True for real numbers; booleans are ints in Python and must NOT pass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def clamp_scale(value: float) -> float:
    """Clamp a scale to the effective [SOLAR_SCALE_MIN, SOLAR_SCALE_MAX] range."""
    return max(SOLAR_SCALE_MIN, min(SOLAR_SCALE_MAX, value))


def effective_scale(configured: float) -> float | None:
    """Resolve the configured value: 0.0 = auto (None), else clamped manual."""
    if configured == _AUTO_SENTINEL:
        return None
    return clamp_scale(configured)


def valid_records(history: list[dict]) -> list[dict]:
    """Filter accuracy records usable for auto-tuning.

    A record is valid when it spans a meaningful window (>= min elapsed
    slots), planned enough solar energy to be measurable (>= min kWh), has
    numeric planned/error values, and did not cross a PV-meter reset during
    its window (reset crossing corrupts the actual-energy measurement).
    """
    valid = []
    for rec in history:
        if not isinstance(rec, dict):
            continue
        elapsed = rec.get("elapsed_slots")
        if not _is_number(elapsed) or elapsed < SOLAR_SCALE_MIN_ELAPSED_SLOTS:
            continue
        planned = rec.get("planned_solar_kwh")
        if not _is_number(planned) or planned < SOLAR_SCALE_MIN_PLANNED_KWH:
            continue
        err = rec.get("solar_error_kwh")
        if not _is_number(err):
            continue
        if rec.get("solar_reset_crossed"):
            continue
        valid.append(rec)
    return valid


def raw_basis_ratio(record: dict) -> float:
    """True unscaled ratio actual/planned for one record, clipped.

    ``actual = planned_scaled + error`` and the stored ``planned`` is the
    *scaled* value, so ``(actual / planned) * scale_used`` recovers the
    ratio the plan would have produced at scale 1.0. Legacy records without
    ``solar_scale_used`` are assumed to have run at scale 1.0.
    """
    planned = record.get("planned_solar_kwh")
    err = record.get("solar_error_kwh")
    if not _is_number(planned) or planned <= 0.0:
        return 1.0
    scale_used = record.get("solar_scale_used", 1.0)
    if not _is_number(scale_used):
        scale_used = 1.0
    err_num = err if _is_number(err) else 0.0
    ratio = ((planned + err_num) / planned) * scale_used
    return clamp_scale(ratio)


def auto_tune_scale(history: list[dict]) -> float:
    """EWMA over raw-basis ratios; 1.0 until warmup threshold is reached."""
    valid = valid_records(history)
    if len(valid) < SOLAR_SCALE_WARMUP_RECORDS:
        return 1.0
    ewma = 1.0
    for record in valid:
        ratio = raw_basis_ratio(record)
        ewma += SOLAR_SCALE_EWMA_ALPHA * (ratio - ewma)
    return clamp_scale(ewma)


def resolve_solar_scale(configured: float, history: list[dict]) -> float:
    """Final scale for this run: manual value wins; 0.0 = auto-tune."""
    manual = effective_scale(configured)
    if manual is not None:
        return manual
    return auto_tune_scale(history)
