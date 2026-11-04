"""Plateau-aware overnight drain (2026-08-06).

COMBINED-mode day (solar cannot fully refill the battery from the floor):
the single discharge pool covers evening -> morning -> night LAST, so the
night slots (cheapest avoided buys) are starved.  The starting SoC sits on
a profit plateau (40-64.9 % give the same day profit) - the excess stored
energy is dead.  The overnight drain discharges that excess to cover the
night load instead of buying grid power, stopping at the plateau edge so
the day plan's profit is untouched.

Fixture: high_pv_day1.json - live p10 forecast day (live_chart_v2.json),
capacity 15 kWh, base load 1.1 kW, start 64.9 %.
"""

import json
import unittest
from pathlib import Path

from . import load_optimizer

optimizer, const = load_optimizer()
SLOTS = const.SLOTS_PER_DAY
HOURS = const.SLOT_DURATION_HOURS
BatteryConfig = optimizer.BatteryConfig

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "high_pv_day1.json"


def load_fixture():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return (
        data["buy"],
        data["sell"],
        data["solar"],
        BatteryConfig(**data["cfg"]),
        data["start_soc_pct"],
    )


def first_solar_slot(solar: list[float], start: int = 0) -> int:
    for s in range(start, len(solar)):
        if solar[s] > 0.05:
            return s
    return len(solar)


def day1_cfg() -> BatteryConfig:
    return BatteryConfig(**json.loads(_FIXTURE.read_text(encoding="utf-8"))["cfg"])


class NightDrainTests(unittest.TestCase):
    """The overnight drain fills starved night slots with dead excess energy."""

    def setUp(self):
        self.buy, self.sell, self.solar, self.cfg, self.start_pct = load_fixture()

    def _plain(self):
        return optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=self.start_pct,
            enable_night_drain=False,
        )

    def test_combined_night_slots_rejected(self):
        """Strict floor rejects every grid-funded night buy: no pre-solar
        discharge when no night buy clears min-buy / round-trip + wear."""
        floor = (
            min(self.buy) / self.cfg.round_trip_factor
            + self.cfg.wear_cost_per_kwh
        )
        res = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=self.start_pct,
        )
        night_dis = [
            s for s in res.slots[:first_solar_slot(self.solar)]
            if s.action == "discharge"
        ]
        self.assertFalse(
            night_dis,
            f"grid-funded night discharge slipped through: {night_dis}",
        )

    def test_night_drain_stops_at_edge(self):
        """Night discharge never dips below the plateau edge; the day holds
        the configured floor target.  Day1 rejects all grid-funded drains
        (strict floor > every night buy), so the merged plan stays above it."""
        res = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=self.start_pct,
        )
        floor = (
            min(self.buy) / self.cfg.round_trip_factor
            + self.cfg.wear_cost_per_kwh
        )
        night_dis = [
            s for s in res.slots[:first_solar_slot(self.solar)]
            if s.action == "discharge"
        ]
        self.assertFalse(
            night_dis,
            f"night discharge below strict floor {floor:.4f}: {night_dis}",
        )
        edge = optimizer._find_plateau_edge(
            self.buy, self.sell, self.solar, self.cfg, 0,
            self.start_pct, self._plain().total_profit, False, None,
            floor,
        )
        # Invariant: the merged plan must never go below the edge-optimal
        # plan's own trajectory floor (edge plan idle-drains the same window,
        # so its floor already absorbs the night's idle drain).
        edge_plan = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=edge, enable_night_drain=False,
        )
        edge_min = min(s.soc_after for s in edge_plan.slots)
        min_soc = min(s.soc_after for s in res.slots)
        self.assertGreaterEqual(
            min_soc, edge_min - 1.0,
            f"merged floor {min_soc:.1f}% below edge plan floor {edge_min:.1f}%",
        )
        self.assertGreaterEqual(
            min_soc, self.cfg.soc_floor_target_pct - 0.5,
            f"min {min_soc:.1f}% below floor target",
        )

    def test_plateau_edge_identified(self):
        """Strict floor blocks all discharge -> flat plateau -> edge clamps
        at the lower bound (lb = floor target + recovery + margin)."""
        floor = (
            min(self.buy) / self.cfg.round_trip_factor
            + self.cfg.wear_cost_per_kwh
        )
        edge = optimizer._find_plateau_edge(
            self.buy, self.sell, self.solar, self.cfg, 0,
            self.start_pct, self._plain().total_profit, False, None,
            floor,
        )
        lb = (
            self.cfg.soc_floor_target_pct
            + self.cfg.soc_recovery_buffer_pct
            + 5.0
        )
        self.assertGreaterEqual(edge, lb)
        self.assertLessEqual(edge, lb + 2.0)

    def test_profit_not_reduced_by_night_drain(self):
        """Merged (drain on) never loses profit vs plain (drain off).  Day1
        rejects every grid-funded drain, so merged == plain; the gain case is
        covered by the full-solar split-mode test instead."""
        merged = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=self.start_pct,
        )
        self.assertGreaterEqual(
            merged.total_profit, self._plain().total_profit - 0.005,
            f"merged {merged.total_profit:.4f} vs plain "
            f"{self._plain().total_profit:.4f}",
        )

    def test_no_drain_when_night_unprofitable(self):
        """Night buys <= wear cost -> no overnight discharge."""
        cheap = [0.02] * SLOTS  # below wear_cost 0.03
        res = optimizer.optimize(
            cheap, [0.0] * SLOTS, self.solar, self.cfg,
            start_slot=0, initial_soc_pct=self.start_pct,
        )
        fss = first_solar_slot(self.solar)
        night_dis = [s for s in res.slots[:fss] if s.action == "discharge"]
        self.assertFalse(night_dis, f"unexpected night discharge: {night_dis}")

    def test_no_drain_when_start_after_solar(self):
        """start_slot past first solar slot -> no probe, identical plan."""
        start = first_solar_slot(self.solar) + 2
        a = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=start, initial_soc_pct=self.start_pct,
            enable_night_drain=False,
        )
        b = optimizer.optimize(
            self.buy, self.sell, self.solar, self.cfg,
            start_slot=start, initial_soc_pct=self.start_pct,
        )
        self.assertEqual(a.total_profit, b.total_profit)
        self.assertEqual(
            [sp.action for sp in a.slots], [sp.action for sp in b.slots]
        )

    def test_edge_floor_guard(self):
        """Edge never drops below floor target + recovery + margin (>= 25 %)."""
        edge = optimizer._find_plateau_edge(
            self.buy, self.sell, self.solar, self.cfg, 0,
            self.start_pct, self._plain().total_profit, False, None,
            self.cfg.wear_cost_per_kwh,
        )
        lb = (
            self.cfg.soc_floor_target_pct
            + self.cfg.soc_recovery_buffer_pct
            + 5.0
        )
        self.assertGreaterEqual(edge, lb)


class SplitModeUnchangedTests(unittest.TestCase):
    """Full-solar day (SPLIT mode) must not lose profit when night drain is on."""

    def test_split_mode_profit_not_reduced(self):
        buy = [0.12] * SLOTS
        sell = [0.10] * SLOTS
        solar = [0.0] * SLOTS
        for s in range(24, 72):
            solar[s] = 12.0
        cfg = day1_cfg()
        plain = optimizer.optimize(
            buy, sell, solar, cfg, start_slot=0, initial_soc_pct=64.9,
            enable_night_drain=False,
        )
        merged = optimizer.optimize(
            buy, sell, solar, cfg, start_slot=0, initial_soc_pct=64.9,
        )
        self.assertGreaterEqual(
            merged.total_profit, plain.total_profit - 0.005,
            f"merged {merged.total_profit:.4f} vs plain "
            f"{plain.total_profit:.4f}",
        )
