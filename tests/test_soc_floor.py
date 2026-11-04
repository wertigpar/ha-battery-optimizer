"""SoC floor safeguard tests — discharge reserve (A) + rescue override (B).

Fixture note: the 2026-08-05 live episode (sauna drain → idle-drain dips
3.3-4.0 % below soc_min in tomorrow's plan) predates the repro harness
capture, so the failing day is reproduced deterministically from
BatteryConfig + consts instead of a captured JSON fixture.  The mechanism
is exact: a heavy evening discharge run exhausts the discharge budget, the
battery sits at the floor, and idle drain (~0.1 kW) pulls the projected
trajectory below soc_min for the rest of the night.

RED on current code: discharge budget = current - soc_min (no reserve), so
the evening run drains all the way to the floor and the overnight dip
violates it.  GREEN with A: budget stops at soc_min + reserve.  GREEN with
B: the rescue flips a committed discharge slot into charge_floor when no
free slot exists.
"""

from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig
SLOTS = const.SLOTS_PER_DAY  # 96
HOURS = const.SLOT_DURATION_HOURS  # 0.25


def make_cfg(**overrides) -> BatteryConfig:
    """Sauna-day config: 15 kWh, 15 % floor, 5 % recovery buffer."""
    base = dict(
        capacity_kwh=15.0,
        soc_min=15.0,
        soc_max=100.0,
        soc_recovery_buffer_pct=5.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        base_load_kw=10.0,
        idle_power_kw=0.1,
    )
    base.update(overrides)
    return BatteryConfig(**base)


def flat_prices(price: float, sell: float | None = None) -> list[float]:
    return [price] * SLOTS


def flat_solar(kw: float) -> list[float]:
    return [kw] * SLOTS


# ---------------------------------------------------------------------------
# Flat-day scenario (v0.2.3 production regression, 2026-08): Finnish tariff
# with a genuinely flat day — sell 0.002-0.003 vs buy 0.0839+.  The greedy
# pass planned a morning top-up (16 no-op keep-alive slots at the cheapest
# price) plus an afternoon discharge run that ended below soc_min, forcing
# an emergency charge.  Scenario mirrors the live-captured repro arrays
# (bopt-prod/runmodes.py): weak solar 0.4/1.2/0.5 kW, spot 0.020-0.060.
# ---------------------------------------------------------------------------

def flatday_spot() -> list[float]:
    """96 x 15-min spot prices (€/kWh), flat Finnish winter day."""
    spot: list[float] = []
    for h in range(24):
        p = 0.035
        if 2 <= h < 6:
            p = 0.020
        elif 6 <= h < 9:
            p = 0.045
        elif 9 <= h < 16:
            p = 0.040
        elif 16 <= h < 21:
            p = 0.060
        elif 21 <= h < 23:
            p = 0.030
        spot.extend([p] * 4)
    return spot


def flatday_solar() -> list[float]:
    """96 x 15-min solar forecast (kW), weak winter sun."""
    solar: list[float] = []
    for h in range(24):
        kw = 0.0
        if 7 <= h < 10:
            kw = 0.4
        elif 10 <= h < 15:
            kw = 1.2
        elif 15 <= h < 18:
            kw = 0.5
        solar.extend([kw] * 4)
    return solar


def flatday_cfg() -> BatteryConfig:
    """Prod config (bopt-prod/runmodes.py): 15 kWh, 15 % floor, 5 % buffer."""
    return make_cfg(
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        vat_multiplier=1.255,
        transfer_fee_buy=0.0776,
        sales_commission=0.003,
        base_load_kw=1.1,
        wear_cost_per_kwh=0.03,
        idle_power_kw=0.1,
        enable_soc_safeguard=True,
        soc_recovery_buffer_pct=5.0,
    )


class DischargeReserveTests(unittest.TestCase):
    """A — discharge runs stop at soc_min + recovery buffer."""

    def test_sauna_night_tomorrow_plan_holds_floor(self):
        """Regression: heavy evening discharge + overnight idle drain must not
        project SoC below soc_min.  (Live 2026-08-05: dipped 3.3-4.0 %.)

        Safeguard is OFF here on purpose: the existing safeguard's normal
        keep-alive insert would mask the dip (free slots exist and it fixes
        them), so this test isolates the reserve — the discharge budget must
        leave headroom that makes the dip never happen."""
        cfg = make_cfg(enable_soc_safeguard=False)
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            flat_solar(0.0),
            cfg,
            start_slot=80,
            initial_soc_pct=33.0,
        )
        min_soc_pct = min(s.soc_after for s in result.slots[80:])
        tolerance = 0.5  # percent points
        self.assertGreaterEqual(min_soc_pct, cfg.soc_min - tolerance)

    def test_discharge_ends_at_reserve_not_floor(self):
        """The last discharge slot's SoC must sit at soc_min + buffer, not
        soc_min — so idle drain has headroom before the floor.

        Safeguard OFF: it would otherwise top the battery back up and hide
        where the greedy pass actually stopped.  Full-solar day (SPLIT
        mode): flat 0.12 clears the refill-day wear floor, so the discharge
        runs engage and must stop at the reserve line."""
        cfg = make_cfg(enable_soc_safeguard=False)
        solar = [0.0] * SLOTS
        for s in range(24, 72):  # full solar recharge (like charge-side case)
            solar[s] = 12.0
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            solar,
            cfg,
            start_slot=0,
            initial_soc_pct=33.0,
        )
        discharges = [s for s in result.slots if s.action == "discharge"]
        self.assertTrue(discharges, "expected a discharge run")
        end_of_run = discharges[-1].soc_after
        reserve_line_pct = cfg.soc_min + cfg.soc_recovery_buffer_pct
        tolerance = 0.5  # percent points
        self.assertGreaterEqual(end_of_run, reserve_line_pct - tolerance)

    def test_charge_side_unaffected_by_reserve(self):
        """Full-sun day still fills the battery to soc_max — the reserve
        caps discharge only, never solar charging."""
        cfg = make_cfg()
        solar = [0.0] * SLOTS
        for s in range(24, 72):  # 12 h of strong sun
            solar[s] = 12.0
        result = optimizer.optimize(
            flat_prices(0.12),
            flat_prices(0.10),
            solar,
            cfg,
            start_slot=0,
            initial_soc_pct=20.0,
        )
        max_soc_pct = max(s.soc_after for s in result.slots)
        self.assertGreaterEqual(max_soc_pct, 0.98 * cfg.soc_max)


class RescueOverrideTests(unittest.TestCase):
    """B — committed discharge slot flips to charge_floor when no free slot."""

    def _cfg(self) -> BatteryConfig:
        return make_cfg(base_load_kw=1.0)

    def _committed_discharge_plan(self):
        """Slots 10..44 committed discharge; battery sized so the run ends
        at the floor at slot 39, and the committed (now energy-empty) slots
        40..44 keep draining idle power — the violation forms inside the
        committed window (slot 43) where no free slot exists."""
        cfg = self._cfg()
        plan = {s: "discharge" for s in range(10, 45)}
        draw_per_slot = (
            min(1.0, cfg.max_discharge_kw)
            * HOURS
            / cfg.discharge_efficiency
        )
        idle_per_slot = cfg.idle_drain_per_slot_kwh
        floor_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
        initial_kwh = floor_kwh + 30 * (draw_per_slot + idle_per_slot)
        return cfg, plan, initial_kwh

    def test_rescue_flips_committed_discharge_slot(self):
        cfg, plan, initial = self._committed_discharge_plan()
        inserted = optimizer._apply_soc_safeguard(
            plan,
            flat_prices(0.10),
            flat_solar(1.0),  # net_load > 0 → discharge draws
            flat_solar(0.0),
            cfg,
            start_slot=10,
            initial_soc_kwh=initial,
        )
        # A slot that was committed discharge became charge_floor.
        rescued = [
            s for s in inserted
            if 10 <= s <= 44 and plan.get(s) == "charge_floor"
        ]
        self.assertTrue(rescued, f"no rescue flip; inserted={inserted}")
        # Trajectory must hold the floor after the flip.
        traj = optimizer._simulate_soc_trajectory(
            plan, flat_solar(1.0), flat_solar(0.0), cfg,
            start_slot=10, initial_soc_kwh=initial,
        )
        floor_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
        tolerance = cfg.capacity_kwh * 0.01  # 1 % — slack for float boundary
        self.assertGreaterEqual(min(traj[10:]), floor_kwh - tolerance)

    def test_rescue_plus_normal_insert_cooperate(self):
        """Deep dip: rescue flips the discharge window AND the safeguard
        keeps inserting later free-slot top-ups — at least two inserts,
        one of them a rescue."""
        cfg, plan, initial = self._committed_discharge_plan()
        inserted = optimizer._apply_soc_safeguard(
            plan,
            flat_prices(0.10),
            flat_solar(1.0),
            flat_solar(0.0),
            cfg,
            start_slot=10,
            initial_soc_kwh=initial,
        )
        rescued = [
            s for s in inserted
            if 10 <= s <= 44 and plan.get(s) == "charge_floor"
        ]
        self.assertGreaterEqual(len(inserted), 2, f"inserted={inserted}")
        self.assertTrue(rescued, f"no rescue flip; inserted={inserted}")

    def test_find_rescue_slot_returns_none_without_discharge(self):
        cfg = self._cfg()
        plan = {s: "charge" for s in range(10, 21)}
        self.assertIsNone(
            optimizer._find_rescue_slot(plan, flat_prices(0.10), 10, 30)
        )

    def test_find_rescue_slot_picks_cheapest(self):
        cfg = self._cfg()
        prices = [0.10] * SLOTS
        prices[15] = 0.05  # cheapest discharge slot
        plan = {s: "discharge" for s in range(10, 31)}
        self.assertEqual(
            optimizer._find_rescue_slot(plan, prices, 10, 30), 15
        )

    def test_find_rescue_slot_tie_prefers_closest_to_violation(self):
        cfg = self._cfg()
        plan = {s: "discharge" for s in range(10, 31)}
        self.assertEqual(
            optimizer._find_rescue_slot(plan, flat_prices(0.10), 10, 30), 30
        )


class FlatDayEconomyTests(unittest.TestCase):
    """F3 — flat day: no uneconomical morning top-up, floor target held.

    RED on v0.2.3: greedy charges 16 no-op keep-alive slots at the cheapest
    price (battery already above the floor target there), the afternoon run
    ends at ~14.5 % (below soc_min + buffer), and the leftover floor
    violation pulls an emergency charge.  GREEN: the correction drops the
    cheapest discharge slots so the run's end-SoC clears the floor target —
    no top-up at all (C=0), min >= floor target.
    """

    def test_flat_day_holds_floor_without_uneconomical_topup(self):
        cfg = flatday_cfg()
        buy, sell = optimizer.compute_prices(flatday_spot(), cfg)
        result = optimizer.optimize(
            buy, sell, flatday_solar(), cfg,
            start_slot=0, initial_soc_pct=71.0,
        )
        min_soc = min(s.soc_after for s in result.slots)
        floor_target = cfg.soc_floor_target_pct
        tolerance = 0.5  # percent points
        self.assertGreaterEqual(
            min_soc, floor_target - tolerance,
            f"min {min_soc:.1f}% below floor target {floor_target}%",
        )
        self.assertEqual(
            result.charge_slots, 0,
            f"uneconomical top-up planned: safeguard={result.safeguard_slots}",
        )


class SafeguardEconomyTests(unittest.TestCase):
    """F2 — keep-alive charge inserts must actually add energy.

    RED on v0.2.3: the safeguard picks the cheapest free slot before the
    violation regardless of SoC entering it.  A "charge to N%" byte is a
    no-op when the battery is already above the floor target, so on a
    high-SoC morning the first N inserts waste budget and only a later,
    genuinely deficit slot fixes the dip.  GREEN: only deficit slots are
    candidates — every insert sits below the floor target.
    """

    def test_safeguard_only_inserts_deficit_slots(self):
        cfg = make_cfg(base_load_kw=1.0)
        floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
        draw_per_slot = (
            min(1.0, cfg.max_discharge_kw) * HOURS / cfg.discharge_efficiency
        )
        idle_per_slot = cfg.idle_drain_per_slot_kwh
        floor_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
        # Committed run 10..44 drains to the floor at slot 44; idle drain
        # pushes below soc_min at ~slot 49, where free slots exist.
        initial = floor_kwh + 35 * (draw_per_slot + idle_per_slot) + 0.03
        plan = {s: "discharge" for s in range(10, 45)}
        prices = [0.10] * SLOTS
        for s in range(0, 10):  # cheap morning slots — high SoC, no-op on RED
            prices[s] = 0.05
        net = flat_solar(1.0)  # positive net load -> discharge draws
        solar = flat_solar(0.0)
        inserted = optimizer._apply_soc_safeguard(
            plan, prices, net, solar, cfg,
            start_slot=0, initial_soc_kwh=initial,
        )
        self.assertGreaterEqual(
            len(inserted), 2,
            f"expected cheap-slot no-ops + deficit fix, got {inserted}",
        )
        traj = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=initial,
        )
        for s in inserted:
            soc_before = initial if s == 0 else traj[s - 1]
            self.assertLess(
                soc_before, floor_target_kwh,
                f"no-op charge_floor at slot {s} "
                f"(entering {soc_before:.2f} kWh >= target {floor_target_kwh:.2f} kWh)",
            )


class TrajectoryConsistencyTests(unittest.TestCase):
    """F1 — result-pass SoC must match the simulated trajectory.

    RED on v0.2.3: a solar-surplus slot assigned discharge drains idle power
    twice in the result pass (once in the surplus branch, once after the
    battery_draw line), so the returned soc_after drifts ~0.025 kWh/slot
    below the real trajectory.  The 90 % start reaches the midday surplus
    slots 40-45, exposing the drift.  GREEN: single idle drain — result
    trajectory equals the simulation.
    """

    def test_result_trajectory_matches_simulation(self):
        cfg = flatday_cfg()
        buy, sell = optimizer.compute_prices(flatday_spot(), cfg)
        solar = flatday_solar()
        net = [cfg.base_load_kw - kw for kw in solar]
        initial_pct = 90.0
        result = optimizer.optimize(
            buy, sell, solar, cfg,
            start_slot=0, initial_soc_pct=initial_pct,
        )
        plan = {
            sp.index: sp.action
            for sp in result.slots if sp.action != "none"
        }
        traj = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0,
            initial_soc_kwh=cfg.capacity_kwh * initial_pct / 100.0,
        )
        capacity = cfg.capacity_kwh
        for sp in result.slots:
            self.assertAlmostEqual(
                sp.soc_after / 100.0 * capacity, traj[sp.index],
                delta=0.02,
                msg=f"slot {sp.index} ({sp.action}) drifts from trajectory",
            )

    def test_simulate_charge_target_cap(self):
        optimizer, _ = load_optimizer()
        cfg = optimizer.BatteryConfig(capacity_kwh=15.0, soc_min=20.0, soc_max=100.0,
                                      max_charge_kw=5.0)
        plan = {i: "charge" for i in range(20)}  # enough to exceed the cap
        net = [1.0] * 96
        solar = [0.0] * 96
        traj = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=0.0,
            charge_targets={i: 50 for i in range(20)},
        )
        # capped at 50% of 15kWh = 7.5kWh (after idle drain, never above)
        self.assertLessEqual(traj[19], 7.5 + 1e-9)
        # without the cap, 20 slots would exceed 7.5 kWh
        traj_uncapped = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=0.0,
        )
        self.assertGreater(traj_uncapped[19], 7.5)

    def test_simulate_discharge_floor_cap(self):
        optimizer, _ = load_optimizer()
        cfg = optimizer.BatteryConfig(capacity_kwh=15.0, soc_min=20.0, soc_max=100.0,
                                      max_discharge_kw=5.0)
        # start at 60%, 4 discharge slots with heavy load, user floor 40%
        plan = {i: "discharge" for i in range(4)}
        net = [10.0] * 96  # load >> solar, load-matched discharge
        solar = [0.0] * 96
        traj = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=9.0,  # 60% of 15
            discharge_targets={i: 40 for i in range(4)},
        )
        # floor 40% of 15 = 6 kWh; discharge stops at the floor (minus idle drain)
        self.assertAlmostEqual(traj[3], 5.95, places=4)
        # without the floor, it drains to soc_min (20% = 3 kWh)
        traj_socmin = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=9.0,
        )
        self.assertAlmostEqual(traj_socmin[3], 3.636842105263158, places=4)


    def test_simulate_charge_above_target_is_noop(self):
        """A 'charge to N%' byte is a no-op when SoC is already above N%.

        Physically the firmware never discharges down to a charge target —
        charge@40 from 60% just idles (minus battery idle drain).  RED on
        old code: min(soc + charge - drain, target) forced the battery
        DOWN to 40% (6.0 kWh of 15) in a single slot.  GREEN: only idle
        drain applies; the 60% start stays near 9.0 kWh.
        """
        optimizer, _ = load_optimizer()
        cfg = optimizer.BatteryConfig(
            capacity_kwh=15.0, soc_min=15.0, soc_max=100.0,
            max_charge_kw=5.0, idle_power_kw=0.1,
        )
        plan = {i: "charge" for i in range(4)}
        net = [1.0] * 96
        solar = [0.0] * 96
        traj = optimizer._simulate_soc_trajectory(
            plan, net, solar, cfg, start_slot=0, initial_soc_kwh=9.0,  # 60% of 15
            charge_targets={i: 40 for i in range(4)},  # 40% of 15 = 6.0 kWh
        )
        idle_drain_total = cfg.idle_drain_per_slot_kwh * 4  # 0.025 kWh x 4
        # only idle drain applies — never dragged down to the 40% target
        self.assertGreaterEqual(traj[3], 9.0 - idle_drain_total - 1e-9)
        self.assertGreater(traj[3], 6.0)


class SocGuardMarkerTests(unittest.TestCase):
    """Fix 2 — user discharge floors must RAISE the guard marker.

    RED on min(): a user discharge@40 rule in the window combined with an
    optimizer discharge@15 elsewhere produced marker=15, and the guard's
    global remap rewrote the user's discharge@40 byte to 15 — defeating
    the user's 'never below 40' floor.  GREEN with max(): the user floor
    is a lower bound that raises the marker.
    """

    def _marker(self, *, plan_soc_after: float, user_floor: int | None) -> int:
        """Compute the guard marker for a synthetic plan + user winners."""
        from custom_components.battery_optimizer import coordinator as coord_mod
        from custom_components.battery_optimizer.rules import SlotWinner
        from types import SimpleNamespace
        from unittest import mock

        inst = coord_mod.BatteryOptimizerCoordinator.__new__(
            coord_mod.BatteryOptimizerCoordinator
        )
        slots = [
            SimpleNamespace(action="idle", soc_after=100.0) for _ in range(96)
        ]
        # optimizer discharge elsewhere in the look-ahead window (slot 41)
        slots[41] = SimpleNamespace(action="discharge", soc_after=plan_soc_after)
        winners = [SlotWinner(action="optimizer") for _ in range(96)]
        if user_floor is not None:
            # user discharge@N rule inside the window (slot 44)
            winners[44] = SlotWinner(action="discharge", soc_target=user_floor)
        inst._last_result = SimpleNamespace(slots=slots)
        inst._last_user_winners = winners
        inst._build_battery_config = lambda: SimpleNamespace(soc_min=15.0)
        inst._config_int = lambda key, fallback: 120

        with mock.patch.object(
            coord_mod, "_current_slot_index", return_value=40
        ):
            return inst._compute_soc_guard_marker()

    def test_user_floor_raises_marker_above_optimizer_floor(self):
        # optimizer discharge at 15 % in the window; user discharge@40 must
        # win — the marker must be 40, never dragged down to 15.
        marker = self._marker(plan_soc_after=15.0, user_floor=40)
        self.assertEqual(marker, 40)
        self.assertGreaterEqual(marker, 40)  # the user's floor, not below

    def test_user_floor_below_plan_does_not_lower_marker(self):
        # optimizer discharge at 30 %; user discharge@10 must not pull the
        # marker below the plan floor.
        marker = self._marker(plan_soc_after=30.0, user_floor=10)
        self.assertEqual(marker, 30)

    def test_no_user_floor_uses_plan_marker(self):
        marker = self._marker(plan_soc_after=15.0, user_floor=None)
        self.assertEqual(marker, 15)


if __name__ == "__main__":
    unittest.main()
