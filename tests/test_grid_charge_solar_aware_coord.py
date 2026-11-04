"""Coordinator-side tests for the solar-aware grid charging (issue #26).

Covers the three coordinator findings from the final review:

* FIX 1 (HIGH) — the today-only plan must use TODAY's ratio, not the
  min(today, tomorrow), or an overcast *tomorrow* inflates today's charge.
* FIX 3 (MEDIUM) — the p10/p50 dispersion ratio must not be applied on top of
  an already-pessimistic (P10) ``available_soc``.
* FIX 7 (LOW) — the ``detailedForecast`` parse must be skipped entirely when
  the option is off.

The methods are driven against a ``SimpleNamespace`` fake ``self`` (no Home
Assistant runtime), which is the ``load_coordinator`` harness pattern used by
``test_accuracy_skip.py``.

FIX 1 is about *wiring* between two call sites, so it is asserted on the parsed
AST of ``coordinator.py`` rather than by string matching — the branch is
selected by ``if prices_tomorrow is not None:`` and the two ``optimize()``
calls differ only in their keyword arguments, which is precisely what a
textual assertion would get wrong.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from . import load_coordinator, load_optimizer

coord, const = load_coordinator()
optimizer, _opt_const = load_optimizer()
BatteryOptimizerCoordinator = coord.BatteryOptimizerCoordinator

_COORD_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "battery_optimizer" / "coordinator.py"
)

TODAY_SENSOR = "sensor.solcast_today"
TOMORROW_SENSOR = "sensor.solcast_tomorrow"


def _forecast(ratio: float, p50_per_slot: float = 5.0):
    """48 x 30-min slots whose energy-weighted p10/p50 ratio is `ratio`."""
    return [
        {"pv_estimate": p50_per_slot, "pv_estimate10": p50_per_slot * ratio}
        for _ in range(48)
    ]


class _FakeStates:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)


def _fake_self(
    *,
    aware=1.0,
    mode=None,
    today_ratio=0.95,
    tomorrow_ratio=0.35,
):
    """A coordinator stand-in carrying just what the ratio readers touch."""
    config = {
        const.CONF_SOLCAST_TODAY: TODAY_SENSOR,
        const.CONF_SOLCAST_TOMORROW: TOMORROW_SENSOR,
        const.CONF_GRID_CHARGE_SOLAR_AWARE: aware,
    }
    if mode is not None:
        config[const.CONF_SOLAR_FORECAST_MODE] = mode
    states = {
        TODAY_SENSOR: SimpleNamespace(
            attributes={"detailedForecast": _forecast(today_ratio)}
        ),
        TOMORROW_SENSOR: SimpleNamespace(
            attributes={"detailedForecast": _forecast(tomorrow_ratio)}
        ),
    }
    fake = SimpleNamespace(config=config, hass=SimpleNamespace(states=_FakeStates(states)))
    # Bind the real methods so the guards under test are the production ones.
    fake._grid_charge_solar_aware_enabled = (
        BatteryOptimizerCoordinator._grid_charge_solar_aware_enabled.__get__(fake)
    )
    fake._get_solar_confidence_ratio = (
        BatteryOptimizerCoordinator._get_solar_confidence_ratio.__get__(fake)
    )
    fake._solar_confidence_ratio_for_plan = (
        BatteryOptimizerCoordinator._solar_confidence_ratio_for_plan.__get__(fake)
    )
    return fake


class TestConfidenceRatioModeGuard(unittest.TestCase):
    """FIX 3 — do not double-discount a P10-based available_soc."""

    def test_p10_mode_forces_ratio_to_one(self):
        """P10 available_soc is already pessimistic; the ratio adds nothing."""
        fake = _fake_self(aware=1.0, mode=const.SOLAR_FORECAST_P10)
        self.assertEqual(fake._get_solar_confidence_ratio("today"), 1.0)
        self.assertEqual(fake._get_solar_confidence_ratio("tomorrow"), 1.0)
        self.assertEqual(fake._solar_confidence_ratio_for_plan(), 1.0)

    def test_p50_mode_uses_the_real_dispersion_ratio(self):
        fake = _fake_self(aware=1.0, mode=const.SOLAR_FORECAST_P50)
        self.assertAlmostEqual(fake._get_solar_confidence_ratio("today"), 0.95, places=9)
        self.assertAlmostEqual(fake._get_solar_confidence_ratio("tomorrow"), 0.35, places=9)
        # min across the planned days — correct only for a two-day horizon.
        self.assertAlmostEqual(fake._solar_confidence_ratio_for_plan(), 0.35, places=9)

    def test_default_mode_is_p10_so_the_guard_applies_without_config(self):
        """DEFAULT_SOLAR_FORECAST_MODE is P10, so an unconfigured entry is guarded."""
        self.assertEqual(const.DEFAULT_SOLAR_FORECAST_MODE, const.SOLAR_FORECAST_P10)
        fake = _fake_self(aware=1.0, mode=None)
        self.assertEqual(fake._get_solar_confidence_ratio("today"), 1.0)

    def test_p10_mode_would_double_discount_if_the_ratio_were_applied(self):
        """The reason the guard exists, quantified.

        The forecast is already p10, so ``available_soc`` 8.0 kWh *is* the
        pessimistic figure. Re-applying a 0.60 dispersion ratio plans against
        4.8 kWh — 36 % of the 13.3 kWh median that 0.60 implies, a percentile
        Solcast never issues — and lifts the ceiling from 7.0 to the full
        9.45 kWh headroom.
        """
        soc_max, current, available = 15.0, 5.55, 8.0
        cfg = optimizer.BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        guarded = optimizer._case_b_grid_charge(cfg, soc_max, current, available, 1.0)
        double_discounted = optimizer._case_b_grid_charge(
            cfg, soc_max, current, available, 0.60,
        )
        # The raw pessimistic SoC the unguarded path would have planned against.
        self.assertAlmostEqual(available * 0.60, 4.8, places=9)
        self.assertAlmostEqual(guarded, 7.0, places=9)           # soc_max - available
        self.assertAlmostEqual(double_discounted, 9.45, places=9)  # clamped
        self.assertGreater(double_discounted, guarded)


class TestConfidenceRatioDisabledShortCircuit(unittest.TestCase):
    """FIX 7 — skip the forecast parse when the feature is off."""

    def test_disabled_returns_none_without_touching_states(self):
        """No state read at all: the parse is skipped, not merely discarded."""
        fake = _fake_self(aware=0.0, mode=const.SOLAR_FORECAST_P50)
        fake.hass.states = SimpleNamespace(
            get=SimpleNamespace(
                side_effect=AssertionError("states read while the option is off")
            )
        )
        self.assertIsNone(fake._get_solar_confidence_ratio("today"))
        self.assertIsNone(fake._solar_confidence_ratio_for_plan())

    def test_enabled_weight_parses_normally(self):
        """Negative control: the guard is not short-circuiting unconditionally."""
        fake = _fake_self(aware=0.5, mode=const.SOLAR_FORECAST_P50)
        self.assertAlmostEqual(fake._get_solar_confidence_ratio("today"), 0.95, places=9)

    def test_enabled_helper_reads_false_for_degenerate_weights(self):
        for aware in (0.0, -0.5, None, float("nan"), float("inf"), "junk"):
            fake = _fake_self(aware=aware, mode=const.SOLAR_FORECAST_P50)
            self.assertFalse(
                fake._grid_charge_solar_aware_enabled(),
                msg=f"aware={aware!r}",
            )
        for aware in (0.001, 0.5, 1.0):
            fake = _fake_self(aware=aware, mode=const.SOLAR_FORECAST_P50)
            self.assertTrue(
                fake._grid_charge_solar_aware_enabled(),
                msg=f"aware={aware!r}",
            )


class TestTodayOnlyUsesTodaysRatio(unittest.TestCase):
    """FIX 1 — the today-only branch must not borrow tomorrow's forecast."""

    def _optimize_call_kwargs(self):
        """solar_confidence_ratio per optimize() call site in the planner."""
        tree = ast.parse(_COORD_SRC.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "optimize"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            if "solar_confidence_ratio" in kwargs:
                found.append((node.lineno, kwargs))
        self.assertEqual(
            len(found), 2,
            msg=f"expected exactly 2 optimize() call sites with a ratio, "
                f"found {len(found)} at lines {[ln for ln, _ in found]}",
        )
        return found

    def test_today_only_branch_uses_todays_own_ratio(self):
        """The branch that passes `future_grid_charge_needed` plans today only."""
        by_line = dict(self._optimize_call_kwargs())
        today_only_lines = [
            ln for ln, kw in by_line.items()
            if "future_grid_charge_needed" in kw
        ]
        self.assertEqual(
            len(today_only_lines), 1,
            msg="the today-only call site is identified by future_grid_charge_needed",
        )
        expr = by_line[today_only_lines[0]]["solar_confidence_ratio"]
        self.assertIsInstance(expr, ast.Call, msg=ast.dump(expr))
        self.assertEqual(expr.func.attr, "_get_solar_confidence_ratio")
        self.assertEqual(
            [a.value for a in expr.args], ["today"],
            msg=f"today-only plan must use today's ratio, got {ast.dump(expr)}",
        )

    def test_two_day_branch_still_takes_the_min_across_both_days(self):
        """The continuous plan's available_soc spans both days, so min() stays."""
        by_line = dict(self._optimize_call_kwargs())
        two_day_lines = [
            ln for ln, kw in by_line.items()
            if "future_grid_charge_needed" not in kw
        ]
        self.assertEqual(len(two_day_lines), 1)
        expr = by_line[two_day_lines[0]]["solar_confidence_ratio"]
        self.assertIsInstance(expr, ast.Call, msg=ast.dump(expr))
        self.assertEqual(expr.func.attr, "_solar_confidence_ratio_for_plan")

    def test_cost_of_the_old_wiring_is_the_size_of_the_bug(self):
        """An overcast tomorrow used to inflate today's charge 12x.

        headroom 9.45 kWh, available_soc 15.0 (solar fills it). Today's own
        ratio 0.95 leaves 0.75 kWh to buy; min(today, tomorrow)=0.35 says
        almost nothing gets filled and restores the full 9.45 kWh — the #26
        waste, reintroduced through tomorrow's clouds.
        """
        cfg = optimizer.BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        today_only = optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 0.95)
        borrowed_tomorrow = optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 0.35)
        self.assertAlmostEqual(today_only, 0.75, places=9)
        self.assertAlmostEqual(borrowed_tomorrow, 9.45, places=9)
        self.assertGreater(borrowed_tomorrow, today_only * 10)


class TestSolcastCallSiteCountDocstring(unittest.TestCase):
    """FIX 8 — the docstring claimed five call sites; there are six."""

    #: The reviewer's count, verified against the AST in the test below.
    REAL_CALL_SITES = 7

    def test_docstring_matches_the_real_call_site_count(self):
        source = _COORD_SRC.read_text(encoding="utf-8")
        tree = ast.parse(source)
        cls = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
            and n.name == "BatteryOptimizerCoordinator"
        )
        # The count lives on the method that was kept separate from
        # _get_solcast_forecast precisely to avoid touching its call sites.
        fn = next(
            n for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_get_solar_confidence_ratio"
        )
        calls = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_get_solcast_forecast"
        )
        self.assertEqual(
            calls, self.REAL_CALL_SITES,
            msg="if this fails the call-site count changed; update the docstring "
                "word and this constant together",
        )
        self.assertIn(
            f"all {_WORDS[calls]} existing call sites",
            ast.get_docstring(fn),
            msg="docstring word for the call-site count must track the real count",
        )


_WORDS = {
    3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten",
}