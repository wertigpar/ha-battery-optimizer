"""Two-tier Case A floor and proportional hedge (issue #32).

A partial-refill day shaped like the report: flat buy price all day, an
evening peak, no morning peak, the battery starting the day well above
the floor and a PV forecast that cannot refill it from empty (no "split"
mode) but overflows from where it actually is.  Before the fix every
morning slot had to clear the grid-recharge floor (cheapest future buy /
round trip + wear = 1.01 here), so the battery idled at 0.70 while PV
later exported at 0.11.  With the fix the morning load is covered from
the battery (tier 1, floor 0.11/0.81+0.15 = 0.29) because the PV overflow
refills it, and no night grid charge is planned for it.

Night drain is disabled so the pre-solar discharge in the assertions can
only come from the tier-1 floor.
"""
from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig
SLOTS = const.SLOTS_PER_DAY

START = 24  # 06:00


def _day(*, peak_kw: float = 2.0):
    buy = [0.70] * SLOTS
    for s in range(66, 84):          # 16:30-21:00 evening peak
        buy[s] = 1.15
    sell = [0.11] * SLOTS
    solar = [0.0] * SLOTS
    ramp = [0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.5, 2.5]
    for i, kw in enumerate(ramp):     # 07:00-09:00 ramp (load 0.5 kW)
        solar[28 + i] = min(kw, peak_kw)
    for s in range(36, 61):           # 09:00-15:15 plateau
        solar[s] = peak_kw
    for i, kw in enumerate(reversed(ramp)):
        solar[61 + i] = min(kw, peak_kw)
    return buy, sell, solar


def _cfg(**over):
    args = dict(
        capacity_kwh=15.0, soc_min=13.0, soc_max=100.0,
        soc_recovery_buffer_pct=5.0, base_load_kw=0.5,
        max_charge_kw=8.0, max_discharge_kw=10.0,
        charge_efficiency=0.9, discharge_efficiency=0.9,
        wear_cost_per_kwh=0.15, grid_charge_solar_aware=1.0,
    )
    args.update(over)
    return BatteryConfig(**args)


def _run(cfg, *, peak_kw=2.0, soc=70.0, ratio=0.2):
    buy, sell, solar = _day(peak_kw=peak_kw)
    return optimizer.optimize(
        buy, sell, solar, cfg,
        start_slot=START, initial_soc_pct=soc,
        solar_confidence_ratio=ratio, enable_forced_sell=False,
        enable_night_drain=False,
    )


class TestTierOneFloor(unittest.TestCase):
    def test_morning_load_is_covered_from_the_battery(self):
        result = _run(_cfg())
        trace = result.trace
        self.assertFalse(trace["solar_full_recharge"])
        self.assertIsNotNone(trace["case_a_floor_tier1"])
        self.assertLess(trace["case_a_floor_tier1"], trace["case_a_floor"])
        self.assertAlmostEqual(trace["case_a_floor_tier1"], 0.11 / 0.81 + 0.15, places=3)
        actions = {sp.index: sp.action for sp in result.slots}
        # 06:00-08:00: load above solar, before the surplus; buy 0.70 is
        # below the grid floor (1.01) but above the PV floor (0.29).
        morning = [actions[s] for s in range(START, 32)]
        self.assertIn("discharge", morning)
        self.assertGreaterEqual(morning.count("discharge"), 6)
        self.assertGreater(trace["tier1_discharge_kwh"], 0.0)
        self.assertGreater(trace["n_tier1_discharge"], 0)

    def test_tier_one_energy_is_capped_by_the_overflow(self):
        result = _run(_cfg())
        trace = result.trace
        self.assertLessEqual(
            trace["tier1_discharge_kwh"], trace["tier1_refill_kwh"] + 1e-9
        )

    def test_tier_one_discharge_is_not_bought_back_at_night(self):
        # The morning discharge is refilled by PV, so it must not raise the
        # grid-charge need and must not add a night charge slot.
        result = _run(_cfg())
        actions = [sp.action for sp in result.slots[START:]]
        self.assertNotIn("charge", actions)
        self.assertEqual(result.charge_slots, 0)

    def test_no_overflow_means_no_tier_one(self):
        # A weak PV day that never fills the battery: nothing is exported,
        # so nothing refills a prior discharge and the single floor stands.
        result = _run(_cfg(), peak_kw=0.9)
        self.assertFalse(result.trace["solar_full_recharge"])
        trace = result.trace
        self.assertEqual(trace["tier1_refill_kwh"], 0.0)
        self.assertIsNone(trace["case_a_floor_tier1"])
        self.assertEqual(trace["n_tier1_discharge"], 0)
        actions = {sp.index: sp.action for sp in result.slots}
        self.assertNotIn("discharge", [actions[s] for s in range(START, 28)])

    def test_evening_peak_still_discharges_on_the_grid_floor(self):
        result = _run(_cfg())
        actions = {sp.index: sp.action for sp in result.slots}
        evening = [actions[s] for s in range(66, 84)]
        self.assertIn("discharge", evening)

    def test_post_solar_slots_below_the_grid_floor_stay_idle(self):
        # 17:30-18:00 and the late night at 0.70: no overflow comes after
        # them, so they are tier 2 and 0.70 does not clear 1.01.
        result = _run(_cfg())
        actions = {sp.index: sp.action for sp in result.slots}
        self.assertNotIn("discharge", [actions[s] for s in range(84, 96)])

    def test_default_weight_keeps_the_single_floor(self):
        # The feature is part of solar-aware grid charging: off by default.
        result = _run(_cfg(grid_charge_solar_aware=0.0))
        trace = result.trace
        self.assertIsNone(trace["case_a_floor_tier1"])
        self.assertEqual(trace["n_tier1_discharge"], 0)
        self.assertIsNone(trace["hedge_value_kwh"])
        actions = {sp.index: sp.action for sp in result.slots}
        self.assertNotIn("discharge", [actions[s] for s in range(START, 32)])


class TestProportionalHedge(unittest.TestCase):
    CFG = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
    ARGS = (CFG, 15.0, 5.55, 13.0, 0.2)   # full 9.45, median ceiling 2.0

    def test_hedge_value_caps_the_ceiling_above_the_median(self):
        got = optimizer._case_b_grid_charge(
            *self.ARGS, hedge_net_cost=0.56, hedge_net_benefit=0.22,
            hedge_value_kwh=1.5,
        )
        self.assertAlmostEqual(got, 3.5, places=9)

    def test_zero_hedge_value_is_the_median_ceiling(self):
        got = optimizer._case_b_grid_charge(
            *self.ARGS, hedge_net_cost=0.10, hedge_net_benefit=0.90,
            hedge_value_kwh=0.0,
        )
        self.assertAlmostEqual(got, 2.0, places=9)

    def test_large_hedge_value_cannot_exceed_the_pessimistic_ceiling(self):
        got = optimizer._case_b_grid_charge(
            *self.ARGS, hedge_net_cost=0.10, hedge_net_benefit=0.90,
            hedge_value_kwh=50.0,
        )
        self.assertAlmostEqual(got, 9.45, places=9)

    def test_optimize_reports_the_hedge_value(self):
        result = _run(_cfg())
        trace = result.trace
        # Evening benefit (1.15-0.15)*0.81-0.70 = 0.11 is below the cost
        # 0.70-0.11*0.9 = 0.60, so no evening kWh is worth insuring.
        self.assertEqual(trace["hedge_value_kwh"], 0.0)


if __name__ == "__main__":
    unittest.main()
