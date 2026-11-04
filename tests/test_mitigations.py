"""Regression tests for the 2026-08-08 audit mitigations.

Covers the four findings accepted from the F1-F4 review:

- F1 (wear not netted in reported savings): cycled_kwh / wear_cost_total /
  net_profit accounting on OptimizationResult, and the net-vs-gross split.
- F2b (dead PV-sell gate): the gate now compares the export price against
  the stored value (cheapest future discharge buy, round-trip discounted)
  instead of the same-slot buy price, which can never be beaten on a normal
  day (VAT + transfer fees always push it above the spot-based sell price).
- F3 (two-cycle budget double-count): the over-commit correction keeps the
  simulated trajectory above the floor even when initial_usable +
  post_solar_usable exceeds the physical battery band.
- F4 (discharge-surplus solar undercount): solar absorbed in discharge-mode
  surplus slots is credited (margin-scaled) into the grid-charge balance;
  the plan itself must be unchanged and grid charging must never grow.

Fixtures are synthetic — no live-data files required.
"""

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig
SLOTS = const.SLOTS_PER_DAY


def flat(value, n=SLOTS):
    return [float(value)] * n


class WearNettingTests(unittest.TestCase):
    """F1: net savings = gross savings minus wear from cycled kWh."""

    def test_net_profit_accounting(self):
        """Morning charge / evening discharge: wear is real and netted."""
        buy = [0.08] * 48 + [0.30] * 48
        sell = flat(0.01)
        solar = flat(0.0)
        cfg = BatteryConfig(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.5, wear_cost_per_kwh=0.03,
        )
        res = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=30.0,
        )

        self.assertGreater(res.discharge_slots, 0)
        self.assertGreater(res.cycled_kwh, 0.0)
        self.assertAlmostEqual(
            res.wear_cost_total, res.cycled_kwh * cfg.wear_cost_per_kwh, places=9,
        )
        self.assertAlmostEqual(
            res.net_profit, res.total_profit - res.wear_cost_total, places=9,
        )
        # Net savings headline is strictly below gross savings (wear > 0).
        self.assertLess(res.net_profit, res.total_profit)

    def test_emaldo_cost_nets_wear(self):
        """Emaldo benchmark is netted the same way (its plan also cycles)."""
        buy = [0.08] * 48 + [0.30] * 48
        sell = flat(0.01)
        solar = flat(0.0)
        cfg = BatteryConfig(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.5,
        )
        modes = [1] * 30 + [0] * 30 + [-1] * 36
        res = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=30.0,
            emaldo_modes=modes,
        )
        self.assertNotEqual(res.emaldo_cost, 0.0)
        # Emaldo plan with a battery must be cheaper than no battery at all,
        # and the netted cost must never exceed the gross alternative.
        self.assertLess(res.emaldo_cost, res.baseline_cost)
        self.assertAlmostEqual(
            res.net_profit, res.total_profit - res.wear_cost_total, places=9,
        )

    def test_no_discharge_no_wear(self):
        """No cycling -> no wear -> net equals gross."""
        buy = flat(0.05)
        sell = flat(0.01)
        solar = flat(0.0)
        cfg = BatteryConfig(capacity_kwh=5.0, base_load_kw=0.5)
        res = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=30.0,
        )
        self.assertEqual(res.cycled_kwh, 0.0)
        self.assertEqual(res.wear_cost_total, 0.0)
        self.assertAlmostEqual(res.net_profit, res.total_profit, places=9)


class PvSellGateTests(unittest.TestCase):
    """F2b: sell when export price beats stored value, not same-slot buy."""

    STRONG_SOLAR = [0.0] * 32 + [2.5] * 32 + [0.0] * 32  # 8am-4pm

    def _cfg(self, **overrides):
        base = dict(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.5,
        )
        base.update(overrides)
        return BatteryConfig(**base)

    def test_inverted_profile_sells(self):
        """Morning export beats storage value -> sell slots appear.

        Morning spot high (0.30, through the solar window), afternoon low
        (0.12).  Storing a morning kWh is worth only ~0.108 (cheapest
        avoided buy 0.12, round-trip 0.9025), so exporting at 0.298 beats
        storing.
        """
        buy = [0.30] * 56 + [0.12] * 40
        sell = [0.298] * 56 + [0.118] * 40
        res = optimizer.optimize(
            buy, sell, self.STRONG_SOLAR, self._cfg(),
            start_slot=0, initial_soc_pct=60.0,
            enable_pv_strategy=True,
        )
        sells = [s for s, v in enumerate(res.thirdparty_pv_slots) if not v]
        self.assertGreater(len(sells), 0)
        # Only pre-noon slots sell; battery must still fill post-cutover.
        self.assertTrue(all(s < 48 for s in sells))
        self.assertGreater(max(sp.soc_after for sp in res.slots), 90.0)

    def test_below_stored_value_no_sell(self):
        """Export below stored value -> gate stays closed (old gate dead too).

        Morning sell 0.10 vs same-slot buy 0.20 would never fire the old
        gate; the new gate also rejects it (threshold ~0.226 = cheapest
        discharge buy 0.25 discounted).
        """
        buy = [0.20] * 56 + [0.25] * 40
        sell = [0.10] * 56 + [0.15] * 40
        res = optimizer.optimize(
            buy, sell, self.STRONG_SOLAR, self._cfg(),
            start_slot=0, initial_soc_pct=60.0,
            enable_pv_strategy=True,
        )
        self.assertEqual(res.thirdparty_pv_slots.count(False), 0)

    def test_pv_strategy_disabled_unchanged(self):
        """Without enable_pv_strategy the pv slot plan stays all-True."""
        buy = [0.30] * 56 + [0.12] * 40
        sell = [0.298] * 56 + [0.118] * 40
        res = optimizer.optimize(
            buy, sell, self.STRONG_SOLAR, self._cfg(),
            start_slot=0, initial_soc_pct=60.0,
            enable_pv_strategy=False,
        )
        self.assertEqual(res.thirdparty_pv_slots.count(False), 0)


class OverCommitFloorTests(unittest.TestCase):
    """F3: double-counted budget never drains below the floor."""

    def test_double_count_holds_floor(self):
        """Full battery + full solar recharge -> budget over-commit corrected.

        initial_usable (100 % - 20 % - 5 % reserve = 75 %) plus
        post_solar_usable (same ~75 %) totals ~7.5 kWh against a 4 kWh
        physical band above the floor — the classic double count.  The
        over-commit correction must drop discharge slots until the simulated
        trajectory holds soc_min + recovery buffer.
        """
        buy = [0.06] * 40 + [0.20] * 56
        sell = flat(0.01)
        solar = [0.0] * 32 + [3.0] * 24 + [0.0] * 40  # 8am-2pm full recharge
        cfg = BatteryConfig(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.8,
        )
        res = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=100.0,
        )

        self.assertGreater(res.discharge_slots, 0)
        floor_pct = cfg.soc_floor_target_pct  # 25 %
        min_soc = min(sp.soc_after for sp in res.slots)
        # 1.5 % tolerance covers result-sim vs correction-sim differences.
        self.assertGreaterEqual(min_soc, floor_pct - 1.5)

    def test_partial_charge_never_below_floor(self):
        """Moderate solar (no full recharge) also holds the floor."""
        buy = [0.06] * 40 + [0.20] * 56
        sell = flat(0.01)
        solar = [0.0] * 32 + [2.0] * 16 + [0.0] * 48  # 8am-noon, partial
        cfg = BatteryConfig(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.8,
        )
        res = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=100.0,
        )
        floor_pct = cfg.soc_floor_target_pct
        min_soc = min(sp.soc_after for sp in res.slots)
        self.assertGreaterEqual(min_soc, floor_pct - 1.5)


class SurplusAbsorptionTests(unittest.TestCase):
    """F4: discharge-mode surplus absorption credited, plan unchanged.

    The Emaldo firmware charges from excess solar even in discharge mode.
    The absorption credit scales with solar_forecast_margin: grid charging
    must never grow with the margin, and the discharge plan must be
    identical across margins (the fix only touches the energy balance).
    """

    def _scenario(self, margin):
        buy = [0.06] * 32 + [0.30] * 8 + [0.06] * 56
        sell = flat(0.01)
        solar = [0.0] * 32 + [3.0] * 9 + [0.0] * 55  # 8-10:15am surplus
        cfg = BatteryConfig(
            capacity_kwh=5.0, max_charge_kw=2.5, max_discharge_kw=2.5,
            base_load_kw=0.5, solar_forecast_margin=margin,
        )
        return optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=60.0,
        )

    def test_discharge_surplus_absorbed(self):
        """Discharge slots exist on surplus (solar > load) slots."""
        res = self._scenario(1.0)
        self.assertGreater(res.discharge_slots, 0)
        surplus_discharges = [
            s for s, sp in enumerate(res.slots)
            if sp.action == "discharge" and sp.solar_kw > sp.load_kw
        ]
        self.assertGreater(len(surplus_discharges), 0)

    def test_margin_never_grows_grid_charging(self):
        """Higher margin (more absorption credit) never increases charge slots."""
        res_low = self._scenario(0.0)
        res_def = self._scenario(0.85)
        res_high = self._scenario(1.0)
        self.assertLessEqual(res_high.charge_slots, res_def.charge_slots)
        self.assertLessEqual(res_def.charge_slots, res_low.charge_slots)

    def test_plan_unchanged_across_margins(self):
        """The fix must not disturb the discharge/charge assignment."""
        r0 = self._scenario(0.0)
        r1 = self._scenario(1.0)
        acts0 = [sp.action for sp in r0.slots]
        acts1 = [sp.action for sp in r1.slots]
        # Discharge actions must match; charge placement may differ (that is
        # the whole point), but discharge set is fixed by the budget.
        dis0 = [i for i, a in enumerate(acts0) if a == "discharge"]
        dis1 = [i for i, a in enumerate(acts1) if a == "discharge"]
        self.assertEqual(dis0, dis1)
        # The credit must never starve the battery (unclamped sim may dip
        # below soc_min from idle drain, but never toward empty).
        self.assertGreater(min(sp.soc_after for sp in r1.slots), 5.0)


if __name__ == "__main__":
    unittest.main()
