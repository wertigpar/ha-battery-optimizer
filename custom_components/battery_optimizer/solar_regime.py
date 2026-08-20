"""Solar regime — durable no-refill detector for the discharge economic gate.

Pure module (no Home Assistant imports): standalone-loadable via importlib,
matching the runtime_state.py precedent.  The four tuning constants are
imported from ``.const`` when the package is loaded; a local fallback keeps
the module importable standalone for unit tests.

Semantics
---------
The planner's solar *scale* (solar_scale.py) compensates short-term
over-forecasting.  This module answers a different question: can the battery
refill from solar within the coming days — or is every stored kWh going to be
replaced by a future *grid* purchase (winter, snow on panels)?

The signal is the scaled-forecast solar energy of one day as a fraction of
the usable band (``soc_max - soc_min`` of the user's own battery — generic,
no absolute values, works for any capacity).  A slow per-day EWMA (τ ≈ 10
days) plus hysteresis and a consecutive-day debounce guarantee that a
transient cloudy week never engages the gate; only a *durable* low-production
regime (winter months, snow lasting weeks) flips ``engaged`` on.

State
-----
``{date, ewma, engaged, low_days, high_days}``.  ``date`` guards the
once-per-day update (same date → the input state object is returned
unchanged, so the caller can skip persistence).  ``ewma`` seeds at 1.0 and
``engaged`` at False → cold start is always gate-off until sustained low
fraction is observed.  Both flip directions require ``DEBOUNCE_DAYS``
consecutive days on the same side of the threshold, so the gate cannot
oscillate or roam on a single bad day.
"""

from __future__ import annotations

try:
    from .const import (
        SOLAR_REGIME_EWMA_ALPHA,
        SOLAR_REGIME_ENGAGE,
        SOLAR_REGIME_DISENGAGE,
        SOLAR_REGIME_DEBOUNCE_DAYS,
    )
except ImportError:  # standalone unit-test loading — mirrors const.py defaults
    SOLAR_REGIME_EWMA_ALPHA = 0.1
    SOLAR_REGIME_ENGAGE = 0.25
    SOLAR_REGIME_DISENGAGE = 0.40
    SOLAR_REGIME_DEBOUNCE_DAYS = 3

_FRACTION_CAP = 2.0  # sunny surplus days may exceed 1.0 of the band


def _is_number(value) -> bool:
    """True for real numbers; booleans are ints in Python and must NOT pass."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def default_state() -> dict:
    """Cold-start state: gate off until sustained low fraction is observed."""
    return {
        "date": "",
        "ewma": 1.0,
        "engaged": False,
        "low_days": 0,
        "high_days": 0,
    }


def deserialize(data) -> dict:
    """Validate persisted JSON state; default_state() on garbage/missing keys."""
    if not isinstance(data, dict):
        return default_state()
    date = data.get("date", "")
    if not isinstance(date, str):
        date = ""
    ewma = data.get("ewma", 1.0)
    if not _is_number(ewma):
        ewma = 1.0
    engaged = bool(data.get("engaged", False))
    low_days = data.get("low_days", 0)
    high_days = data.get("high_days", 0)
    if not isinstance(low_days, int) or isinstance(low_days, bool) or low_days < 0:
        low_days = 0
    if not isinstance(high_days, int) or isinstance(high_days, bool) or high_days < 0:
        high_days = 0
    return {
        "date": date,
        "ewma": ewma,
        "engaged": engaged,
        "low_days": low_days,
        "high_days": high_days,
    }


def update_regime(state: dict, forecast_kwh: float, usable_band_kwh: float, today: str) -> dict:
    """One daily EWMA step; returns the SAME object when today already updated.

    ``forecast_kwh`` is the day's total scaled solar forecast (kWh).
    ``usable_band_kwh`` is the user's ``(soc_max - soc_min) * capacity``.
    When ``today`` matches the state's date the input object is returned
    unchanged (identity check lets the caller skip the persistence write) —
    the once-per-day guarantee that prevents intra-day seesaw.
    """
    if today == state.get("date"):
        return state
    if not _is_number(usable_band_kwh) or usable_band_kwh <= 0.0:
        # No sensible band — cannot classify; keep current state, mark the day.
        return {**state, "date": today}
    fraction = max(0.0, min(_FRACTION_CAP, forecast_kwh / usable_band_kwh))

    ewma = state.get("ewma", 1.0)
    if not _is_number(ewma):
        ewma = 1.0
    ewma += SOLAR_REGIME_EWMA_ALPHA * (fraction - ewma)

    low_days = state.get("low_days", 0)
    high_days = state.get("high_days", 0)
    engaged = bool(state.get("engaged", False))

    # Hysteresis dead-zone: EWMA between ENGAGE and DISENGAGE moves no
    # counter, so the thresholds never fight over the boundary.
    if ewma < SOLAR_REGIME_ENGAGE:
        low_days += 1
        high_days = 0
        if low_days >= SOLAR_REGIME_DEBOUNCE_DAYS:
            engaged = True
    elif ewma > SOLAR_REGIME_DISENGAGE:
        high_days += 1
        low_days = 0
        if high_days >= SOLAR_REGIME_DEBOUNCE_DAYS:
            engaged = False

    return {
        "date": today,
        "ewma": ewma,
        "engaged": engaged,
        "low_days": low_days,
        "high_days": high_days,
    }