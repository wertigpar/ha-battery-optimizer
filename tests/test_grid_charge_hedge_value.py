"""Value gate for the confidence hedge in the Case B grid charge (issue #32)."""
from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig

CFG = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
# soc_max 15.0, current 5.55 -> full headroom 9.45; p50 solar leaves 2.0 kWh
# of headroom (available 13.0); p10/p50 ratio 0.2 hedges up to the full 9.45.
ARGS = (CFG, 15.0, 5.55, 13.0, 0.2)


class TestHedgeValueGate(unittest.TestCase):
    def test_no_prices_means_unchanged_hedge(self):
        self.assertAlmostEqual(optimizer._case_b_grid_charge(*ARGS), 9.45, places=9)

    def test_hedge_dropped_when_it_costs_more_than_it_saves(self):
        got = optimizer._case_b_grid_charge(
            *ARGS, hedge_net_cost=0.56, hedge_net_benefit=0.22
        )
        # Falls back to what the median forecast leaves unfilled.
        self.assertAlmostEqual(got, 2.0, places=9)

    def test_hedge_kept_when_shortfall_pays_more_than_displaced_pv(self):
        got = optimizer._case_b_grid_charge(
            *ARGS, hedge_net_cost=0.10, hedge_net_benefit=0.90
        )
        self.assertAlmostEqual(got, 9.45, places=9)

    def test_break_even_drops_the_hedge(self):
        got = optimizer._case_b_grid_charge(
            *ARGS, hedge_net_cost=0.30, hedge_net_benefit=0.30
        )
        self.assertAlmostEqual(got, 2.0, places=9)

    def test_weight_zero_ignores_the_gate(self):
        off = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.0)
        got = optimizer._case_b_grid_charge(
            off, 15.0, 5.55, 13.0, 0.2, hedge_net_cost=9.0, hedge_net_benefit=0.0
        )
        self.assertAlmostEqual(got, 9.45, places=9)

    def test_partial_weight_blends_toward_the_median_ceiling(self):
        half = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.5)
        got = optimizer._case_b_grid_charge(
            half, 15.0, 5.55, 13.0, 0.2, hedge_net_cost=0.56, hedge_net_benefit=0.22
        )
        self.assertAlmostEqual(got, 9.45 * 0.5 + 2.0 * 0.5, places=9)

    def test_confident_forecast_is_not_affected(self):
        # ratio 1.0: hedge ceiling already equals the median ceiling.
        got = optimizer._case_b_grid_charge(
            CFG, 15.0, 5.55, 13.0, 1.0, hedge_net_cost=0.56, hedge_net_benefit=0.22
        )
        self.assertAlmostEqual(got, 2.0, places=9)


if __name__ == "__main__":
    unittest.main()
