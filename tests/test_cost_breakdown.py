"""Tests for plan-cost subcost breakdown attributes (v0.2.4).

The four plan-cost sensors (optimizer / emaldo, today / tomorrow) expose
their main value's subcosts as attributes.  The breakdown math lives in two
pure functions in ``optimizer.py`` so it can be tested without HA:

- ``optimizer_plan_cost_breakdown(result)`` — grid_cost + wear_cost == state,
  where state == ``baseline_cost - net_profit``.
- ``emaldo_plan_cost_breakdown(result)`` — emaldo_grid_cost +
  emaldo_wear_cost == state, where state == ``emaldo_cost``.
"""

from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
OptimizationResult = optimizer.OptimizationResult
SLOTS = const.SLOTS_PER_DAY  # 96


def make_result(**overrides) -> OptimizationResult:
    """Hand-built result; every field has a dataclass default."""
    base = dict(
        baseline_cost=10.0,
        total_profit=2.5,
        wear_cost_total=0.7,
        cycled_kwh=3.0,
        net_profit=1.8,
        emaldo_cost=4.8,
        emaldo_grid_cost=4.2,
        emaldo_wear_total=0.6,
        emaldo_cycled_kwh=2.0,
    )
    base.update(overrides)
    return OptimizationResult(**base)


def make_cfg(**overrides):
    """Minimal config for the optimize() integration test."""
    base = dict(
        capacity_kwh=15.0,
        soc_min=15.0,
        soc_max=100.0,
        soc_recovery_buffer_pct=5.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        base_load_kw=10.0,
        idle_power_kw=0.1,
        wear_cost_per_kwh=0.10,
    )
    base.update(overrides)
    return optimizer.BatteryConfig(**base)


def flat_prices(price: float) -> list[float]:
    return [price] * SLOTS


def flat_solar(kw: float) -> list[float]:
    return [kw] * SLOTS


# ---------------------------------------------------------------------------
# Pure breakdown functions
# ---------------------------------------------------------------------------

class TestOptimizerBreakdown(unittest.TestCase):
    def test_matches_state(self):
        """grid_cost + wear_cost == baseline_cost - net_profit (sensor state)."""
        result = make_result()
        attrs = optimizer.optimizer_plan_cost_breakdown(result)
        self.assertEqual(attrs["grid_cost"], 7.5)  # 10.0 - 2.5 (gross)
        self.assertEqual(attrs["wear_cost"], 0.7)
        self.assertEqual(attrs["cycled_kwh"], 3.0)
        state = result.baseline_cost - result.net_profit  # 8.2
        self.assertAlmostEqual(
            attrs["grid_cost"] + attrs["wear_cost"], state, places=9
        )

    def test_rounding(self):
        """Money attrs rounded to 4 dp, cycled_kwh to 3 dp."""
        result = make_result(
            baseline_cost=12.345678,
            total_profit=1.234567,
            wear_cost_total=0.123456,
            cycled_kwh=1.23456,
        )
        attrs = optimizer.optimizer_plan_cost_breakdown(result)
        self.assertEqual(attrs["grid_cost"], round(12.345678 - 1.234567, 4))
        self.assertEqual(attrs["wear_cost"], round(0.123456, 4))
        self.assertEqual(attrs["cycled_kwh"], round(1.23456, 3))

    def test_zero_result(self):
        """Default/empty result yields zeros, no crash."""
        attrs = optimizer.optimizer_plan_cost_breakdown(OptimizationResult())
        for key, value in attrs.items():
            self.assertEqual(value, 0.0, f"{key} should be 0.0")
        self.assertNotEqual(attrs, {})  # contract: full breakdown key set


class TestEmaldoBreakdown(unittest.TestCase):
    def test_matches_state(self):
        """emaldo_grid_cost + emaldo_wear_cost == emaldo_cost (sensor state)."""
        result = make_result()
        attrs = optimizer.emaldo_plan_cost_breakdown(result)
        self.assertEqual(attrs["emaldo_grid_cost"], 4.2)
        self.assertEqual(attrs["emaldo_wear_cost"], 0.6)
        self.assertEqual(attrs["emaldo_cycled_kwh"], 2.0)
        self.assertAlmostEqual(
            attrs["emaldo_grid_cost"] + attrs["emaldo_wear_cost"],
            result.emaldo_cost,
            places=9,
        )

    def test_rounding(self):
        """Money attrs rounded to 4 dp, cycled_kwh to 3 dp."""
        result = make_result(
            emaldo_grid_cost=3.345678,
            emaldo_wear_total=0.456789,
            emaldo_cycled_kwh=2.34567,
        )
        attrs = optimizer.emaldo_plan_cost_breakdown(result)
        self.assertEqual(attrs["emaldo_grid_cost"], round(3.345678, 4))
        self.assertEqual(attrs["emaldo_wear_cost"], round(0.456789, 4))
        self.assertEqual(attrs["emaldo_cycled_kwh"], round(2.34567, 3))

    def test_zero_result(self):
        """Default/empty result yields zeros, no crash."""
        attrs = optimizer.emaldo_plan_cost_breakdown(OptimizationResult())
        for key, value in attrs.items():
            self.assertEqual(value, 0.0, f"{key} should be 0.0")
        self.assertNotEqual(attrs, {})  # contract: full breakdown key set


# ---------------------------------------------------------------------------
# optimize() populates the new emaldo fields (construction wiring)
# ---------------------------------------------------------------------------

class TestOptimizeEmaldoFields(unittest.TestCase):
    def test_populates_emaldo_fields(self):
        """A discharge run under Emaldo modes must record cycled/wear/grid cost,
        and total emaldo_cost must equal grid_cost + wear."""
        cfg = make_cfg()
        modes = [0] * SLOTS
        for s in range(36, 48):  # noon discharge run
            modes[s] = -1
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            flat_solar(0.0),
            cfg,
            start_slot=0,
            initial_soc_pct=50.0,
            emaldo_modes=modes,
        )
        self.assertGreater(result.emaldo_cycled_kwh, 0)
        self.assertGreater(result.emaldo_wear_total, 0)
        self.assertAlmostEqual(
            result.emaldo_wear_total,
            cfg.wear_cost_per_kwh * result.emaldo_cycled_kwh,
            places=9,
        )
        self.assertAlmostEqual(
            result.emaldo_grid_cost + result.emaldo_wear_total,
            result.emaldo_cost,
            places=9,
        )

    def test_fields_zero_without_modes(self):
        """emaldo_modes=None (default) leaves all three fields at 0.0."""
        cfg = make_cfg()
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            flat_solar(0.0),
            cfg,
            start_slot=0,
            initial_soc_pct=50.0,
        )
        self.assertEqual(result.emaldo_grid_cost, 0.0)
        self.assertEqual(result.emaldo_wear_total, 0.0)
        self.assertEqual(result.emaldo_cycled_kwh, 0.0)


class TestEmaldoPvAwareSim(unittest.TestCase):
    """PV-aware emaldo benchmark: surplus charges battery in any mode (PV on).

    Default (enable_pv_strategy=False) implies pv_flags all-True; a charge-mode
    slot with a solar surplus must therefore charge the battery from solar and
    import nothing from grid, and a FULL battery must export the surplus
    instead of grid-charging.
    """

    def setUp(self):
        self.cfg = make_cfg()

    def test_charge_mode_with_solar_surplus_is_grid_free(self):
        """Surplus solar (PV on) charges battery; no grid import in charge mode."""
        base_load = self.cfg.base_load_kw            # 10 kW
        solar = 20.0                                 # 10 kW surplus per slot
        modes = [0] * SLOTS
        for s in range(0, 8):
            modes[s] = 1                             # morning charge window
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            flat_solar(solar),
            self.cfg,
            start_slot=0,
            initial_soc_pct=10.0,
            emaldo_modes=modes,
        )

        # Charge-mode slots with PV on must not import from grid when a 10 kW
        # surplus dwarfs the 5 kW charge rate: solar fills the battery, the
        # rest exports.  (Cycled kWh only counts discharge — solar charging is
        # wear-free and invisible there, so the signal is grid import ~ 0.)
        self.assertEqual(result.emaldo_import_kwh, 0.0)

    def test_full_battery_charge_mode_exports_surplus(self):
        """Battery at 100% + charge mode + surplus → export, zero grid import."""
        modes = [1] * 8                              # charge slots from slot 0
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            flat_solar(20.0),
            self.cfg,
            start_slot=0,
            initial_soc_pct=100.0,
            emaldo_modes=modes,
        )
        self.assertEqual(result.emaldo_import_kwh, 0.0)
        self.assertGreater(result.emaldo_export_kwh, 0.0)
        self.assertEqual(result.emaldo_cycled_kwh, 0.0)
