"""Solar balance — self-sufficiency context derived from accuracy records.

Pure module (no Home Assistant imports), standalone-loadable via importlib.
Consumes the integration's own persisted accuracy history
(``battery_optimizer_accuracy.json`` records): every record carries the
external solar counter delta for the window since the previous run
(``actual_solar_kwh``) and the run timestamp (``last_run``).  Consecutive
records partition time, so summing ``actual_solar_kwh`` per calendar date
approximates that date's total solar production (periods without optimizer
runs are simply not counted).

The report is display/context only — it never gates planning.  The solar
regime (forecast-based, predictive) and the price-driven Case B arbitrage
remain the decision makers; this answers *"is the home a structural net
importer or exporter?"* for dashboards and diagnostics.
"""

from __future__ import annotations

from datetime import date, datetime

_MIN_DAYS = 5  # fewer sampled days -> None (unknown), matching the auto-tune
_MAX_DAYS = 7  # trailing window


def _is_number(value) -> bool:
    """True for real numbers; booleans are ints in Python and must NOT pass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _record_date(record: dict) -> date | None:
    """Calendar date of a record from its ``last_run`` ISO timestamp."""
    raw = record.get("last_run")
    if not isinstance(raw, str):
        return None
    try:
        # Python 3.10's fromisoformat does not accept the trailing 'Z' that
        # HA emits for UTC timestamps — normalize it to an explicit offset.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.date()


def solar_balance_report(
    records: list[dict] | None,
    base_load_kw: float | None,
    usable_band_kwh: float | None,
    window_days: int = _MAX_DAYS,
    min_days: int = _MIN_DAYS,
) -> dict | None:
    """Average daily solar production vs base load over the trailing window.

    Returns None (sensor shows ``unknown``) when fewer than ``min_days``
    distinct dates have valid production records — too little data to trust,
    the same policy the auto-tune uses.  Returns a dict otherwise:

    ``avg_daily_solar_kwh``  mean of per-date sums of ``actual_solar_kwh``
    ``days_sampled``         distinct dates contributing
    ``window_start`` / ``window_end``  oldest/newest contributing date
    ``daily_base_load_kwh``  ``base_load_kw x 24`` (None when base load unset)
    ``self_sufficiency``     ``avg / daily_base_load`` (< 1 = net importer)
    ``battery_days``         ``band / daily_base_load`` — days a full battery
                             alone covers the base load
    ``usable_band_kwh``      passed-through band for display

    A record is valid when ``actual_solar_kwh`` is numeric and ``last_run``
    parses; garbage records are skipped, never fatal.
    """
    if not records:
        return None
    by_day: dict[date, float] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        actual = rec.get("actual_solar_kwh")
        if not _is_number(actual) or actual < 0.0:
            continue
        d = _record_date(rec)
        if d is None:
            continue
        by_day[d] = by_day.get(d, 0.0) + actual

    if len(by_day) < min_days:
        return None
    ordered = sorted(by_day)
    ordered = ordered[-window_days:]
    days = ordered[-window_days:]
    avg = sum(by_day[d] for d in days) / len(days)

    report: dict = {
        "avg_daily_solar_kwh": round(avg, 1),
        "days_sampled": len(days),
        "window_start": ordered[0].isoformat(),
        "window_end": ordered[-1].isoformat(),
        "usable_band_kwh": usable_band_kwh,
    }
    if _is_number(base_load_kw) and base_load_kw > 0.0:
        daily_load = base_load_kw * 24.0
        report["daily_base_load_kwh"] = round(daily_load, 1)
        report["self_sufficiency"] = round(max(avg / daily_load, 0.0), 3)
        if _is_number(usable_band_kwh) and usable_band_kwh > 0.0:
            report["battery_days"] = round(usable_band_kwh / daily_load, 2)
    else:
        report["daily_base_load_kwh"] = None
    return report


__all__ = ["solar_balance_report", "_MIN_DAYS", "_MAX_DAYS"]