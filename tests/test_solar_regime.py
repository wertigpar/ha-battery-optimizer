"""Unit tests for solar_regime.py — durable no-refill gate state machine.

Standalone: loads solar_regime.py via importlib (no package context; the
module's ``try/except ImportError`` falls back to the const.py mirror
defaults).  Run: ``python tests/test_solar_regime.py`` (plain asserts, no
pytest dependency).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE = os.path.join(
    _HERE, "..", "custom_components", "battery_optimizer", "solar_regime.py"
)

_spec = importlib.util.spec_from_file_location("solar_regime", _MODULE)
sr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sr)

_BAND = 12.75  # e.g. 15 kWh × (100% − 15%)


def _days(state, fractions, start=1):
    """Run update_regime over consecutive days; returns final state."""
    for i, frac in enumerate(fractions, start=start):
        state = sr.update_regime(state, frac * _BAND, _BAND, f"2026-08-{i:02d}")
    return state


def _assert(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ── cold start ────────────────────────────────────────────────────────
state = sr.default_state()
_assert(state == {
    "date": "", "ewma": 1.0, "engaged": False, "low_days": 0, "high_days": 0,
}, "default_state shape")
_assert(not state["engaged"], "cold start gate OFF")

# ── once-per-day identity (no intra-day double update) ────────────────
s1 = sr.update_regime(state, 1.0, _BAND, "2026-08-01")
s2 = sr.update_regime(s1, 5.0, _BAND, "2026-08-01")
_assert(s2 is s1, "same-date update returns identical object")
_assert(s1["date"] == "2026-08-01", "date stamped")

# ── sunny days never engage ───────────────────────────────────────────
s = _days(sr.default_state(), [1.2] * 30)
_assert(not s["engaged"], "sunny month → gate OFF")
_assert(s["ewma"] > 1.15, "sunny ewma drifts up, not down")

# ── cloudy week (transient) never engages ─────────────────────────────
s = _days(sr.default_state(), [0.6] * 7)
_assert(not s["engaged"], "cloudy week → gate OFF")
_assert(s["ewma"] > 0.40, "cloudy-week ewma stays above disengage band")

# ── winter: durable low engages after τ + debounce ────────────────────
s10 = _days(sr.default_state(), [0.1] * 10)
_assert(not s10["engaged"], "winter day 10 → still OFF (τ not elapsed)")
s30 = _days(sr.default_state(), [0.1] * 30)
_assert(s30["engaged"], "winter month → gate ON")
_assert(s30["ewma"] < 0.25, "winter ewma settled below engage threshold")

# ── snow: near-zero fraction engages similarly ────────────────────────
s = _days(sr.default_state(), [0.05] * 30)
_assert(s["engaged"], "snow month → gate ON")

# ── hysteresis: disengage needs 3 high days after a low plateau ───────
base = _days(sr.default_state(), [0.1] * 30)  # engaged, ewma ≈ 0.1
s = sr.update_regime(base, 2.0 * _BAND, _BAND, "2026-09-01")
s = sr.update_regime(s, 2.0 * _BAND, _BAND, "2026-09-02")
_assert(s["engaged"], "2 sunny days → still engaged (debounce)")
_assert(s["high_days"] == 1, "day 1 in hysteresis dead-zone, day 2 counts")
s = sr.update_regime(s, 2.0 * _BAND, _BAND, "2026-09-03")
s = sr.update_regime(s, 2.0 * _BAND, _BAND, "2026-09-04")
_assert(not s["engaged"], "4 sunny days → disengaged")
_assert(s["ewma"] > 0.40, "disengage only after EWMA clears hysteresis top")

# ── re-dip resets the disengage counter (no flip-flop) ────────────────
mid = {
    "date": "2026-09-02", "ewma": 0.26, "engaged": True,
    "low_days": 0, "high_days": 2,
}
s = sr.update_regime(mid, 0.05 * _BAND, _BAND, "2026-09-03")  # ewma → 0.239
_assert(s["high_days"] == 0, "dip below engage resets high-day counter")
_assert(s["low_days"] == 1, "low side starts counting")
_assert(s["engaged"], "single dip cannot disengage")

# ── fraction cap at 2.0 ───────────────────────────────────────────────
s = sr.update_regime(sr.default_state(), 5.0 * _BAND, _BAND, "2026-08-01")
_assert(abs(s["ewma"] - 1.1) < 1e-9, "fraction capped at 2.0")

# ── degenerate band: no classification, date still advances ───────────
s = sr.update_regime(sr.default_state(), 1.0, 0.0, "2026-08-01")
_assert(s["date"] == "2026-08-01", "zero band still advances date")
_assert(s["ewma"] == 1.0, "zero band leaves ewma untouched")

# ── persistence round-trip ────────────────────────────────────────────
s = _days(sr.default_state(), [0.1] * 30)
blob = json.dumps(s)
restored = sr.deserialize(json.loads(blob))
_assert(restored == s, "json round-trip preserves state")
_assert(sr.deserialize(None) == sr.default_state(), "None → cold start")
_assert(sr.deserialize("garbage") == sr.default_state(), "non-dict → cold start")
_assert(sr.deserialize({"ewma": "x"})["ewma"] == 1.0, "bad ewma → default")
_assert(sr.deserialize({"low_days": -3})["low_days"] == 0, "bad low_days → default")

print("ALL solar_regime tests passed")