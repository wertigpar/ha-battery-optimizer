"""Cheap-price opportunistic charge selection (issue #31), pure."""
from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig
SLOTS = const.SLOTS_PER_DAY


def _cfg(**over):
    args = dict(
        capacity_kwh=10.0, soc_min=10.0, soc_max=100.0, base_load_kw=0.5,
        max_charge_kw=4.0, charge_efficiency=0.9,
    )
    args.update(over)
    return BatteryConfig(**args)


def _prices(cheap_from: int, cheap_to: int, cheap: float = 0.0, normal: float = 0.30):
    buy = [normal] * SLOTS
    for s in range(cheap_from, cheap_to):
        buy[s] = cheap
    return buy


def _select(buy, solar=None, *, soc=20.0, now=36, tomorrow=False, only_unknown=True,
            threshold=0.03, plan=None, cfg=None):
    return optimizer.cheap_charge_slots(
        buy, solar or [0.0] * SLOTS, cfg or _cfg(), soc,
        now_slot=now, tomorrow_available=tomorrow,
        only_when_tomorrow_unknown=only_unknown,
        price_threshold=threshold, plan_actions=plan,
    )


class TestSelection(unittest.TestCase):
    def test_free_daytime_window_is_charged_cheapest_first(self):
        # 10:00-16:00 free, 20 % SoC, 8 kWh headroom, 0.9 kWh per slot ->
        # nine slots, taken in slot order among equal prices.
        chosen = _select(_prices(40, 64))
        self.assertEqual(chosen, set(range(40, 49)))

    def test_nothing_when_no_slot_is_cheap_enough(self):
        self.assertEqual(_select(_prices(40, 64, cheap=0.05)), set())

    def test_only_remaining_slots_count(self):
        chosen = _select(_prices(40, 64), now=60)
        self.assertEqual(chosen, {60, 61, 62, 63})

    def test_known_tomorrow_blocks_the_rule_by_default(self):
        self.assertEqual(_select(_prices(40, 64), tomorrow=True), set())

    def test_known_tomorrow_allowed_when_the_option_is_off(self):
        chosen = _select(_prices(40, 64), tomorrow=True, only_unknown=False)
        self.assertEqual(chosen, set(range(40, 49)))

    def test_solar_surplus_reduces_the_amount(self):
        # 4 kW surplus (capped at the charge power) for 4 h = 16 kWh x 0.9 x
        # the 0.85 forecast margin fills the 8 kWh headroom: nothing to buy
        # even at a zero price.
        solar = [0.0] * SLOTS
        for s in range(44, 60):
            solar[s] = 4.5
        self.assertEqual(_select(_prices(40, 64), solar), set())

    def test_partial_solar_leaves_a_partial_need(self):
        solar = [0.0] * SLOTS
        for s in range(44, 52):          # 2.5 kW x 2 h x 0.9 x 0.85 = 3.8 kWh
            solar[s] = 3.0
        chosen = _select(_prices(40, 64), solar)
        self.assertTrue(0 < len(chosen) < 9)

    def test_full_battery_needs_nothing(self):
        self.assertEqual(_select(_prices(40, 64), soc=100.0), set())

    def test_existing_charge_slots_count_and_discharge_slots_are_skipped(self):
        plan = {40: "charge", 41: "discharge"}
        chosen = _select(_prices(40, 64), plan=plan)
        self.assertNotIn(40, chosen)
        self.assertNotIn(41, chosen)
        # 40 already contributes 0.9 kWh, so one slot fewer is selected.
        self.assertEqual(len(chosen), 8)

    def test_cheapest_slots_win_over_earlier_ones(self):
        buy = _prices(40, 64, cheap=0.02)
        buy[60] = 0.0
        buy[61] = -0.01
        chosen = _select(buy, cfg=_cfg(capacity_kwh=2.0))  # 1.6 kWh need
        self.assertEqual(chosen, {60, 61})

    def test_charge_power_zero_selects_nothing(self):
        self.assertEqual(_select(_prices(40, 64), cfg=_cfg(max_charge_kw=0.0)), set())


if __name__ == "__main__":
    unittest.main()
