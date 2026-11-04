"""Regression tests for issue #8 — PV Sell Strategy dead on high-solar days.

Pure optimizer tests (no Home Assistant).  Loads the live source via the
test-package harness so there is no drift from production.

Issue #8 (https://github.com/wertigpar/ha-battery-optimizer/issues/8):
the PV-sell economic gate used to compare the slot's sell price against the
same-slot BUY price.  Buy always includes VAT + transfer fees, so the gate
could never open — and on high-solar days the battery also fills purely from
free solar, leaving no planned discharge slots, which collapsed the old
fallback to `sell_threshold = None` (sell NEVER).  The fix references the
cheapest post-cutover solar-export price instead, so expensive morning solar
now sells while the battery still charges to 100% from midday sun.
"""

import unittest

from . import load_optimizer

opt, _const = load_optimizer()

N = opt.SLOTS_PER_DAY  # 96


def _half_split(morning: float, afternoon: float) -> list[float]:
    """Make a price curve: `morning` for the first half, `afternoon` after."""
    half = N // 2
    return [morning] * half + [afternoon] * (N - half)


def _run_pv_sell(morning_sell: float, afternoon_sell: float, solar: float = 3.0):
    """Run the PV-sell planner on a no-discharge (solar-fills) day.

    `slots=[]` means no planned charge/discharge, which is exactly the
    high-solar-day shape that used to leave the gate dead.
    """
    cfg = opt.BatteryConfig()  # defaults: capacity 5 kWh, soc_max 100%
    slots: list = []
    solar_15min = [solar] * N
    buy_prices = [0.30] * N  # unused in the no-discharge branch
    sell_prices = _half_split(morning_sell, afternoon_sell)
    return opt._plan_pv_sell_slots(
        cfg, slots, solar_15min, buy_prices, sell_prices,
        start_slot=0, initial_soc_kwh=2.0,
    )


class TestPvSellHighSolarNoDischarge(unittest.TestCase):
    def test_morning_sell_fires_when_above_afternoon_floor(self):
        pv = _run_pv_sell(1.0, 0.30)
        # Expensive morning solar now sells to grid ...
        self.assertFalse(pv[10])
        self.assertFalse(pv[40])
        # ... while cheap midday/afternoon solar charges the battery (default).
        self.assertTrue(pv[60])
        # Exactly the pre-cutover window (slots 0..47) is sold.
        self.assertEqual(sum(1 for x in pv if not x), N // 2)

    def test_no_sell_when_morning_below_stored_value(self):
        pv = _run_pv_sell(0.20, 1.0)
        # Morning price is below the cheapest later export → never sells.
        self.assertTrue(pv[10])
        self.assertTrue(pv[40])
        self.assertTrue(pv[60])
        self.assertEqual(sum(1 for x in pv if not x), 0)

    def test_regression_anchor_issue_8(self):
        # Before the fix the no-discharge branch set sell_threshold = None,
        # leaving every slot True — the strategy was dead precisely on the
        # sunny days it targets.  Asserting a morning sell now fires proves
        # the gate is live (reverting the fix makes this fail).
        pv = _run_pv_sell(1.0, 0.30)
        self.assertFalse(pv[10])
        self.assertTrue(pv[60])


if __name__ == "__main__":
    unittest.main()
