"""Forced-sell arbitrage planner tests (Task 1, RED fixtures).

Battery-only manual sell: discharge at peak (wear + round-trip);
PV surplus export belongs exclusively to the 3rd-party PV strategy. Pure-planner tests calling
optimize() with 96-slot fake arrays; conventions from test_pv_sell_gate.py.

Fixture invariant: buy prices flat 0.03 (<= wear_cost 0.03, so greedy
discharge never fires) except where a test deliberately spikes a slot.
Sell peaks 0.20 in slots 34-37, flats 0.08 elsewhere — at 0.08 the
battery gate fails: (0.08 - 0.03) * 0.9025 - 0.03 < 0.02.
"""
from __future__ import annotations

import pytest

from . import load_optimizer

opt, _const = load_optimizer()

N = opt.SLOTS_PER_DAY          # 96
ETA_RT = 0.9025                # charge_efficiency * discharge_efficiency
WEAR = 0.03                    # wear_cost_per_kwh
MIN_PROFIT = 0.02              # forced_sell_min_profit default
WINDOW = (34, 35, 36, 37)


def _cfg(budget=6.0, min_price=0.0, min_profit=MIN_PROFIT, capacity=15.0,
         max_discharge_kw=10.0, max_charge_kw=10.0):
    """Forced-sell config.

    ``max_discharge_kw`` and ``max_charge_kw`` default to 10.0 -- the
    coordinator defaults (DEFAULT_MAX_DISCHARGE_KW / DEFAULT_MAX_CHARGE_KW) --
    because the forced-sell planner derives its per-slot ceilings from them:
    ``max_discharge_per_slot_kwh`` = 10.0 * 0.25 = 2.5 kWh for the sell cap,
    ``max_charge_per_slot_kwh`` = 10.0 * 0.25 = 2.5 kWh for the pre-window
    charge step.  Both are also the bare ``BatteryConfig`` dataclass defaults,
    so the two agree; a test that wants a different per-slot ceiling must say
    so here rather than lean on that default.
    """
    return opt.BatteryConfig(
        forced_sell_enabled=True,
        forced_sell_max_kwh_pd=budget,
        forced_sell_min_profit=min_profit,
        forced_sell_min_price=min_price,
        capacity_kwh=capacity,
        max_discharge_kw=max_discharge_kw,
        max_charge_kw=max_charge_kw,
    )


def _prices(peak_slots=WINDOW, peak=0.20, flat_sell=0.08, flat_buy=0.03,
            buy_block=None, solar=None):
    buy = [flat_buy] * N
    if buy_block:
        lo, hi, price = buy_block
        for s in range(lo, hi):
            buy[s] = price
    sell = [flat_sell] * N
    for s in peak_slots:
        sell[s] = peak
    solar_15 = [0.0] * N
    if solar:
        for s, kw in solar.items():
            solar_15[s] = kw
    return buy, sell, solar_15


def test_disabled_plans_nothing():
    """Enabled flag off -> no sell slots, engine behaviour identical."""
    cfg = opt.BatteryConfig()
    buy, sell, solar = _prices()
    r1 = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    r2 = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    acts1 = [(sp.action, sp.slot_value) for sp in r1.slots]
    acts2 = [(sp.action, sp.slot_value) for sp in r2.slots]
    assert r1.sell_slots == []
    assert acts1 == acts2


def test_battery_window_budget_cap():
    """Budget 6.0, cap 15 (usable 11) -> sell 34,35,36 = 6.0 kWh.

    Slots 34-36 fill budget exactly (2.5/2.5/1.0); slot 37 gets step 0
    and breaks. [34,35,36,37] is impossible with 2.5 kWh steps.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices()
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34, 35, 36]
    assert res.sell_target_kwh == 6.0
    assert abs(res.sell_revenue - 1.2) < 1e-9
    assert res.sell_profit > 0
    assert res.slots[34].action == "force_sell"
    assert res.slots[34].sell_source == "battery"


def test_battery_window_budget_2():
    """Budget 2.0 -> single slot [34], target 2.0."""
    cfg = _cfg(budget=2.0, capacity=15.0)
    buy, sell, solar = _prices()
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34]
    assert res.sell_target_kwh == 2.0


def test_min_price_gate_blocks_low_sell():
    """min_price 0.05, sell peak 0.01 -> no window ever opens."""
    cfg = _cfg(budget=6.0, min_price=0.05, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=WINDOW, peak=0.01)
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == []


def test_battery_gate_profit_threshold():
    """Sell 0.05 (gate -0.01195 < 0.02) -> nothing, window never opens."""
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=WINDOW, peak=0.05)
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == []


def test_no_recharge_slot_after_window():
    """Peak only at slot 95 -> s+1 >= 96 guard -> nothing."""
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(95,))
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == []


def test_surplus_unprofitable_breaks_window():
    """Solar surplus at 36 with failing PV gate closes the window.

    Sells 0.20@34-35, 0.06@36+. PV gate at 36:
    0.06 - 0.08*0.9025 = -0.0122 < 0.02 -> break. Result [34,35].
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        peak=0.20,
        flat_sell=0.06,
        buy_block=(36, 96, 0.08),
        solar={36: 1.7},
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34, 35]
    assert 36 not in res.sell_slots


def test_case_a_light_discharge_sells_remainder_continues():
    """Buy spike 0.50@35 -> greedy discharge at 35 (house cover only).

    Discharge covers 0.5 kW x 0.25 h = 0.125 kWh, leaving 2.375 of the
    2.5 slot cap sellable.  Sell 2.5@34, then 2.375@35; window continues.
    [34, 35], target 4.875.

    Capacity raised from 5.0 to 15.0 so the per-slot budget is NOT what stops
    slot 35.  At capacity 5 the old start-of-window budget (3.0 kWh) capped
    slot 35 at 0.5 and the Case-A remainder was masked by the energy limit;
    the budget is now read at the sell slot, which makes that confound worse,
    not better.  With 15 kWh the sell is limited only by the Case-A headroom
    this test is about: 2.5 - 0.125 = 2.375.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        buy_block=(35, 36, 0.50),
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34, 35]
    assert res.sell_target_kwh == pytest.approx(4.875)
    assert abs(res.slots[34].sell_kwh - 2.5) < 1e-9
    assert abs(res.slots[35].sell_kwh - 2.375) < 1e-9
    assert res.slots[35].sell_source == "battery"


def test_case_a_full_rate_discharge_still_breaks():
    """12 kW base load -> discharge uses 3.0 kWh >= 3.0 slot cap.

    max_discharge_kw 12.0 gives a 3.0 kWh per-slot ceiling
    (12.0 * 0.25), which the plan's own 3.0 kWh discharge fills exactly.
    Nothing sellable in the slot -> legacy break: [34] only.
    """
    cfg = opt.BatteryConfig(
        forced_sell_enabled=True,
        forced_sell_max_kwh_pd=6.0,
        forced_sell_min_profit=MIN_PROFIT,
        forced_sell_min_price=0.0,
        capacity_kwh=15.0,
        base_load_kw=12.0,
        max_discharge_kw=12.0,
    )
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        buy_block=(35, 36, 0.50),
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34]
    assert res.slots[34].action == "force_sell"
    assert res.slots[35].action == "discharge"
    assert 35 not in res.sell_slots


def test_case_a_solar_surplus_discharge_still_skips():
    """Surplus solar at discharge slot -> battery busy, no sell headroom.

    net_load <= 0 -> legacy skip/break, no sell at 35: [34] only.
    """
    cfg = _cfg(budget=6.0, capacity=5.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        buy_block=(35, 36, 0.50),
        solar={35: 2.0},
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34]
    assert res.slots[34].action == "force_sell"
    assert res.slots[35].action == "discharge"
    assert 35 not in res.sell_slots


def test_window_limited_by_soc_at_the_sell_slot():
    """60 % start -> [34, 35], target 4.1, energy read per slot.

    The trajectory carries idle drain into the window: it reads 54.33 % at
    slot 33 (8.15 kWh of a 15 kWh pack) and 54.00 % at slot 35.  Budget read
    at each slot: 8.15 - 3.0 (soc_min) - 1.0 (reserve) = 4.15 kWh at the first
    slot, so slot 34 takes the full 2.5 per-slot ceiling; slot 35 then has
    8.10 - 2.5 (already withdrawn) - 3.0 - 1.0 = 1.6 kWh left.

    The window itself still caps this at two slots (2 x 2.5 = 5.0); the old
    start-of-window reading happened to land on 5.0 too, by coincidence — the
    start SoC minus the floor and the reserve, with none of the idle drain
    that has already happened by slot 34.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices()
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=60.0)
    assert res.sell_slots == [34, 35]
    assert res.sell_target_kwh == pytest.approx(4.1)
    assert res.slots[34].sell_kwh == pytest.approx(2.5)
    assert res.slots[35].sell_kwh == pytest.approx(1.6)


def test_battery_window_profit_formula():
    """Peak 0.20@34-35 only, buys flat 0.03.  The gate keeps this window.

    Revenue 1.0 over 5.0 kWh.  ``sell_profit`` is the gate's own margin
    (``cost_keeping - cost_selling``), not the old c_ref closed form: with
    buys flat at the wear floor the tail has no better use for the energy
    than the grid at 0.20, so the gate prices the sale at 0.85 SEK -- the
    displaced value of the 5.0 kWh it would otherwise have bought at 0.03
    plus the round-trip, against the 1.0 SEK of revenue, less the 0.15 SEK
    of wear the five kWh of discharge causes.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35))
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34, 35]
    assert abs(res.sell_revenue - 1.0) < 1e-9
    assert res.sell_profit == pytest.approx(0.85)


def test_sell_planner_disabled_by_default():
    """Bare BatteryConfig() defaults: no forced selling."""
    cfg = opt.BatteryConfig()
    buy, sell, solar = _prices()
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == []


def test_battery_tier_sells_despite_solar_surplus():
    """Solar 1.7@34 does not create a separate 'pv' sell.

    Surplus does not change the battery tier: the trajectory reads 88.2 % at
    slot 34 (4.41 kWh of a 5 kWh pack), so the sellable budget there is
    4.41 - 1.0 (soc_min) - 1.0 (reserve) = 2.41 kWh and the first slot takes
    all of it from the battery, idles, action force_sell.  Revenue 0.482;
    profit ((0.20 - 0.03) * 0.9025 - 0.03) * 2.41 = 0.29745425. no pv entry.

    The per-slot cap here is 2.5; the budget is the tighter of the two, which
    is what the previous 2.5 figure was hiding — with the start-of-window
    reading the cap bound exactly at 2.5.
    """
    cfg = _cfg(budget=6.0, capacity=5.0)
    buy, sell, solar = _prices(
        peak_slots=(34,),
        flat_sell=0.08,
        solar={34: 1.7},
    )
    assert opt.SLOT_IDLE is not None
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34]
    assert res.sell_target_kwh == pytest.approx(2.41)
    assert res.slots[34].sell_kwh == pytest.approx(2.41)
    assert res.slots[34].sell_source == "battery"
    assert res.slots[34].slot_value == opt.SLOT_IDLE
    assert res.slots[34].action == "force_sell"
    assert abs(res.sell_revenue - 0.482) < 1e-9
    # Gate margin (cost_keeping - cost_selling), including the wear the sale
    # itself causes: the tail re-plan prices the 2.41 kWh against the 0.08 buy
    # it would otherwise need at slot 34+, less the 0.03 wear on the discharge.
    assert res.sell_profit == pytest.approx(0.392332, abs=1e-5)


def test_battery_gate_unprofitable_no_selling():
    """Flat sell 0.06 -> battery gate fails everywhere, no window.

    Battery gate at every slot: (0.06 - 0.03) * 0.9025 - 0.03 =
    -0.002925 < 0.02 -> no sell slots. Greedy discharge 37-95
    (buy 0.08 > wear) is harmless to the plan.
    """
    cfg = _cfg(budget=6.0, capacity=5.0)
    buy, sell, solar = _prices(
        peak_slots=(),
        flat_sell=0.06,
        buy_block=(37, 96, 0.08),
        solar={36: 1.7},
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == []


def test_direct_helper_battery_only():
    """Direct helper call, battery-only tier.

    Battery tier sells 34-35, budget 0.0 = unlimited -> every entry
    (2.5, "battery"). No pv source exists anywhere in the plan.

    The window is two slots, not four, because the baseline plan drains the
    pack hard before it (the trajectory reads 77.5 % at slot 34 and 60.7 % at
    slot 35 of a 15 kWh pack): each slot's 2.5 kWh comes out of the battery it
    is sold from, and once two are gone the third slot has nothing above the
    floor plus the reserve.  That is the budget being read at the slot, which
    is the behaviour under test here.
    """
    cfg = opt.BatteryConfig(
        forced_sell_enabled=True,
        forced_sell_max_kwh_pd=0.0,
        forced_sell_min_profit=MIN_PROFIT,
        forced_sell_min_price=0.0,
        capacity_kwh=15.0,
        max_discharge_kw=10.0,
    )
    buy, sell, solar = _prices()
    baseline = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, baseline.slots,
        start_slot=0, round_trip_factor=ETA_RT,
    )
    assert list(plan) == [34, 35]
    assert plan[34] == (2.5, "battery")
    assert plan[35] == (2.5, "battery")
    assert all(tier == "battery" for kwh, tier in plan.values())
    assert all(0 < kwh <= 2.5 for kwh, tier in plan.values())


def test_charge_planner_cheap_buy_before_spike():
    """Cheap 0.01 buy at 30-33 powers the 34-35 battery sells.

    Sell planner with flat sell 0.06 keeps the window shut before the
    spike: (0.06-0.03)*0.9025-0.01 = 0.0171 < 0.02. The engine's own sells are
    2.5 + 1.6 = 4.1 kWh (the slot-read budget; see
    test_window_limited_by_soc_at_the_sell_slot), and the charge helper is
    handed that shape literally. Charge helper buys 2.5 at slot 30: headroom
    6.0 (15-9), battery need 5.0, gate
    (0.20-0.03)*0.9025-0.01 = 0.1434 >= 0.02.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        flat_sell=0.06,
        buy_block=(30, 34, 0.01),
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=60.0)
    assert res.sell_slots == [34, 35]
    assert res.sell_target_kwh == pytest.approx(4.1)
    plan = opt._plan_forced_sell_charge(
        cfg, buy, sell,
        {34: (2.5, "battery"), 35: (2.5, "battery")},
        start_slot=0, start_soc_kwh=9.0, round_trip_factor=ETA_RT,
    )
    assert plan == {30: 2.5}
    assert res.slots[30].action == "charge"
    assert res.slots[30].slot_value == opt._soc_to_charge_target(cfg.soc_max)


def test_charge_planner_gate_blocks_expensive_buy():
    """Buy 0.15 pre-window: charge gate (0.0034 < 0.02) no-ops.

    Sells still happen at the spike; no charge slot anywhere in [0,34).
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        buy_block=(0, 34, 0.15),
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=60.0)
    assert res.sell_slots == [34, 35]
    # 0.15 is above wear, so the plan greedily discharges through the whole
    # pre-window and arrives at slot 34 already low (27.2 %); the sell budget
    # is read there and yields 2.5 + 2.43125.
    assert res.sell_target_kwh == pytest.approx(4.93125)
    plan = opt._plan_forced_sell_charge(
        cfg, buy, sell,
        {34: (2.5, "battery"), 35: (2.5, "battery")},
        start_slot=0, start_soc_kwh=9.0, round_trip_factor=ETA_RT,
    )
    assert plan == {}
    assert all(sp.action != "charge" for sp in res.slots[0:34])


def test_charge_planner_skips_pv_only_sells():
    """Non-battery-tier entries (defensive) yield no charge.

    Literal {34: (0.3, "pv")} input: battery_kwh <= 0.05 -> {}.
    Battery-only engine still sells the slot budget (2.41 kWh) from battery.
    """
    cfg = _cfg(budget=6.0, capacity=5.0)
    buy, sell, solar = _prices(
        peak_slots=(34,),
        flat_sell=0.08,
        solar={34: 1.7},
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34]
    assert res.sell_target_kwh == pytest.approx(2.41)
    assert res.slots[34].sell_source == "battery"
    plan = opt._plan_forced_sell_charge(
        cfg, buy, sell,
        {34: (0.3, "pv")},
        start_slot=0, start_soc_kwh=5.0, round_trip_factor=ETA_RT,
    )
    assert plan == {}
    assert all(sp.action != "charge" for sp in res.slots[0:34])


def test_charge_planner_no_headroom_when_full():
    """Battery already full (headroom 0) -> helper returns {}."""
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35, 36))
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots == [34, 35, 36]
    plan = opt._plan_forced_sell_charge(
        cfg, buy, sell,
        {34: (2.5, "battery"), 35: (2.5, "battery"), 36: (1.0, "battery")},
        start_slot=0, start_soc_kwh=15.0, round_trip_factor=ETA_RT,
    )
    assert plan == {}
    assert all(sp.action != "charge" for sp in res.slots[0:34])


def test_charge_step_scales_with_max_charge_kw():
    """The pre-window charge step is sized from the configured charge power.

    ``max_charge_kw=4.0`` -> ``max_charge_per_slot_kwh`` = 1.0 kWh per slot.
    With 2.0 kWh of battery sell to replenish and 6.0 kWh of headroom the
    step is capped at the 1.0 kWh ceiling: ``{30: 1.0}``.  Restoring the old
    literal ``min(2.5, required_kwh)`` yields ``{30: 2.0}`` instead, so this
    pins the derivation to the config value rather than a frozen 2.5.
    """
    cfg = _cfg(budget=6.0, capacity=15.0, max_charge_kw=4.0)
    buy, sell, solar = _prices(
        peak_slots=(34, 35),
        flat_sell=0.06,
        buy_block=(30, 34, 0.01),
    )
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=60.0)
    assert res.sell_slots == [34, 35]
    plan = opt._plan_forced_sell_charge(
        cfg, buy, sell,
        {34: (1.0, "battery"), 35: (1.0, "battery")},
        start_slot=0, start_soc_kwh=9.0, round_trip_factor=ETA_RT,
    )
    assert plan == {30: 1.0}


# ---------------------------------------------------------------------------
# Per-slot sell ceiling is derived from max_discharge_kw, not a frozen 2.5.
# ---------------------------------------------------------------------------


def _idle_baseline(load_kw=0.0, action="idle", soc_after=100.0):
    """96-slot baseline plan; action=discharge drives the Case-A branch.

    ``soc_after`` is populated by the optimizer for every slot of a real plan;
    the forced-sell budget reads that trajectory, so a synthetic plan that
    leaves it at the dataclass default of 0.0 would report a flat battery.
    100 % keeps these tests measuring the per-slot ceiling, which is what they
    are about, and never the budget.
    """
    return [
        opt.SlotPlan(
            index=s, action=action, slot_value=opt.SLOT_IDLE,
            buy_price=0.03, sell_price=0.20 if s in WINDOW else 0.08,
            load_kw=load_kw, soc_after=soc_after,
        )
        for s in range(N)
    ]


def _sell_step(cfg, baseline=None):
    """kWh the planner stages at slot 34 (unlimited budget + full battery)."""
    buy, sell, _solar = _prices()
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, baseline if baseline is not None else _idle_baseline(),
        start_slot=0, round_trip_factor=ETA_RT,
    )
    return plan.get(34, (0.0, ""))[0]


def test_default_discharge_power_still_yields_2_5_kwh_per_slot():
    """Behaviour-preservation proof.

    The coordinator's default is DEFAULT_MAX_DISCHARGE_KW = 10.0, so a default
    -configured user must still see exactly 2.5 kWh per 15-min slot -- the value
    that used to be hardcoded. Fails if the planner stops honouring
    max_discharge_kw, and if DEFAULT_MAX_DISCHARGE_KW ever moves off 10.0.
    """
    assert _const.DEFAULT_MAX_DISCHARGE_KW == 10.0
    cfg = _cfg(budget=0.0, max_discharge_kw=_const.DEFAULT_MAX_DISCHARGE_KW)
    assert cfg.max_discharge_per_slot_kwh == 2.5
    assert _sell_step(cfg) == 2.5


def test_per_slot_ceiling_tracks_max_discharge_kw():
    """Non-default discharge power is honoured instead of assumed 2.5.

    Emaldo's manual-selling mode moves energy at the configured discharge
    power over the slot, so the per-slot sell ceiling must follow it:
    10.0 kW -> 2.5 kWh, 5.0 kW -> 1.25 kWh, 20.0 kW -> 5.0 kWh.
    """
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=10.0)) == 2.5
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=5.0)) == 1.25
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=20.0)) == 5.0
    # A deliberately low 2.5 kW is honoured -> 0.625 kWh/slot.  Before the
    # per-slot family was derived from power, the frozen 2.5 kWh literal hid
    # this gap: the dataclass default read 2.5 kW while the sell cap used
    # 2.5 kWh, so the two silently agreed.
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=2.5)) == 0.625


def test_case_a_headroom_and_slot_cap_share_one_power_basis():
    """Case-A headroom is measured against the same cap it subtracts from.

    Slot 34 is a full-rate greedy-discharge slot at 12 kW net load with a
    non-default max_discharge_kw. The plan's own discharge is
    min(net_load, max_discharge_kw) * SLOT_DURATION_HOURS, which equals the
    whole per-slot ceiling, so nothing is left to sell. The old frozen 2.5 kWh
    cap staged 1.325 kWh at 4.7 kW -- sell energy the inverter cannot move.
    """
    for kw in (4.7, 5.0, 8.0, 10.0, 12.0):
        cfg = _cfg(budget=0.0, max_discharge_kw=kw)
        staged = _sell_step(cfg, _idle_baseline(load_kw=12.0, action="discharge"))
        assert staged == 0.0, f"max_discharge_kw={kw} staged {staged}"
        assert staged <= cfg.max_discharge_per_slot_kwh
        assert min(12.0, cfg.max_discharge_kw) * opt.SLOT_DURATION_HOURS == \
            cfg.max_discharge_per_slot_kwh


def test_case_a_headroom_respects_partial_ceiling():
    """A light discharge leaves exactly the remainder sellable.

    Load 0.5 kW at max_discharge_kw 10.0 -> 0.125 kWh used by the plan's
    discharge, leaving 2.5 - 0.125 = 2.375 kWh: headroom and ceiling on one
    power basis. At max_discharge_kw 2.5 the same slot yields
    0.625 - 0.125 = 0.5 kWh.
    """
    baseline = _idle_baseline(load_kw=0.5, action="discharge")
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=10.0), baseline) == 2.375
    assert _sell_step(_cfg(budget=0.0, max_discharge_kw=2.5), baseline) == 0.5


# ---------------------------------------------------------------------------
# E2 regression fixture — issue #23 comment 6061738993.
# Solar is zeroed and fees are zeroed so the fixture isolates the price
# valuation defect rather than the reporter's hardware.
#
# The fixture's job is the ORDERING: the cheapest forward buy from the sell
# slot must sit AFTER the evening load peak, so the planner's
# cheapest-forward-buy reference price skips the period where the house is
# actually paying a lot.  The reporter's day had exactly that shape.
# ---------------------------------------------------------------------------


def _surplus_case():
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "tests" / "fixtures" / "surplus_sell_case.json"
    )
    data = json.loads(path.read_text())
    return data


def _surplus_cfg(enabled=True):
    """Fixture config. Budget 6.0 kWh, deliberately above the usable energy.

    With the budget set to the sale size the planner would stop on the budget
    and the case would prove nothing about stored energy; at 6.0 the binding
    constraint is ``start_soc_kwh - soc_min_kwh - 1.0``, so the sale is a
    real withdrawal of stored energy.

    ``wear_cost_per_kwh`` lives in the fixture JSON (0.5) and cannot carry a
    comment.  Caveat for whoever edits it: a sweep of 0.03 -> 0.66 all return
    ``sell_slots == [63, 64]`` with the same 2.6 kWh target — wear scales
    ``sell_profit`` linearly and does NOT pin the window.  The window only
    moves at wear >= 0.69, where slot 63 stops passing the gate and the
    planner jumps straight to the evening peak at slots 68-71.  If you change
    wear, re-check ``sell_slots``.
    """
    data = _surplus_case()
    return opt.BatteryConfig(
        forced_sell_enabled=enabled,
        forced_sell_max_kwh_pd=6.0,
        forced_sell_min_profit=0.02,
        forced_sell_min_price=0.0,
        **data["cfg"],
    )


def _surplus_run(enabled=True):
    data = _surplus_case()
    return opt.optimize(
        data["buy"], data["sell"], data["solar"], _surplus_cfg(enabled),
        initial_soc_pct=data["start_soc_pct"],
    )


def test_surplus_case_cheapest_forward_buy_is_after_the_peak():
    """The precondition the defect needs, asserted rather than assumed.

    From each sell slot the cheapest forward buy is 0.60 at slot 85
    (21:15), which is AFTER the 2.55 evening peak at slots 68-84.  So the
    reference price the gate uses is not a price the house ever pays before
    the sale is settled.
    """
    buy = _surplus_case()["buy"]
    for s in (63, 64):
        cheapest = min(range(s + 1, 96), key=lambda c: buy[c])
        assert buy[cheapest] == 0.60
        assert cheapest == 85, f"slot {s}: argmin moved to {cheapest}"
        assert cheapest > 84, "argmin must land after the evening peak"
    # The peak really is between the sell and that reference price.
    assert max(buy[65:85]) == 2.55


def test_surplus_case_sell_slot_is_not_the_day_maximum():
    """Second half of the fixture precondition, asserted not assumed.

    Selling at the day's maximum sell price would be obviously correct and
    the gate would not be able to mis-evaluate it, so the fixture would
    prove nothing.  ``max(sell) = 2.30`` at slots 68-84; the sell slots
    63-64 only reach 1.45.
    """
    sell = _surplus_case()["sell"]
    day_max = max(sell)
    day_max_slots = [i for i, v in enumerate(sell) if v == day_max]
    assert day_max == pytest.approx(2.30)
    assert day_max_slots[:1] == [68]
    for s in (63, 64):
        assert sell[s] < day_max
        assert s not in day_max_slots


def test_surplus_sell_case_is_window_limited_not_budget_limited():
    """The 2.6 kWh window is set by the WINDOW -- and the gate now refuses it.

    ``_plan_forced_sell_slots`` still sizes each sell slot at
    ``min(per_slot_cap, available_at_this_slot, budget - total)`` where
    ``per_slot_cap = max_discharge_kw * 0.25``.  With this fixture that is
    1.3 kWh per slot and the window is exactly two slots, so the window
    ceiling is ``2 x 1.3 = 2.6``.  At the fixture's start SoC the trajectory
    reads 84.5 % at slot 63 (8.45 kWh of a 10 kWh pack), leaving 5.95 kWh
    sellable there and 4.45 at slot 64 -- far above the 1.3 per-slot ceiling,
    so the binding constraint is the WINDOW, not budget and not energy.

    What changed is the DECISION, not the nomination: the gate re-plans the
    tail and finds the withdrawn 2.6 kWh was worth more where it sat (the
    2.55 evening peak at slots 68-84 displaces the 1.45 sale), so the whole
    window is refused.  The nomination is unchanged; only the keep/refuse
    answer moved, and it moved in the direction the gate exists to move it.
    """
    data = _surplus_case()
    cfg = data["cfg"]
    per_slot_cap = cfg["max_discharge_kw"] * 0.25
    assert per_slot_cap == pytest.approx(1.3)

    # The nomination is still window-limited: two slots at the per-slot cap.
    off = _surplus_run(enabled=False)  # pre-correction trajectory
    cfg_on = _surplus_cfg(enabled=True)
    plan = opt._plan_forced_sell_slots(
        cfg_on, data["buy"], data["sell"],
        off.slots, 0, cfg_on.round_trip_factor,
        total_slots=opt.SLOTS_PER_DAY,
    )
    assert sorted(plan) == [63, 64]
    assert plan[63][0] == pytest.approx(per_slot_cap)
    assert plan[64][0] == pytest.approx(per_slot_cap)
    assert sum(k for k, _ in plan.values()) < 6.0

    # Energy decoupled from the window: a higher start SoC gives the sell
    # slots more energy to draw on and the nomination does not grow.
    for start_soc in (51.0, 80.0, 100.0):
        base = opt.optimize(
            data["buy"], data["sell"], data["solar"], _surplus_cfg(enabled=False),
            initial_soc_pct=start_soc,
        )
        loaded = opt._plan_forced_sell_slots(
            cfg_on, data["buy"], data["sell"],
            base.slots, 0, cfg_on.round_trip_factor,
            total_slots=opt.SLOTS_PER_DAY,
        )
        assert sorted(loaded) == [63, 64]
        assert sum(k for k, _ in loaded.values()) == pytest.approx(2 * per_slot_cap)

    # The gate refuses: the nomination is real but the decision says no.
    res = _surplus_run(enabled=True)
    assert res.sell_slots == []
    assert res.sell_target_kwh == 0.0
    assert res.sell_revenue == 0.0
    assert res.sell_profit == 0.0
    with_sale, keeping = _tail_delta(
        data["buy"], data["sell"], data["solar"], _surplus_cfg(),
        0, data["start_soc_pct"],
    )
    assert with_sale > keeping, "keeping must be the cheaper side to refuse"
    assert with_sale - keeping > 0.5, "margin must not be a rounding artefact"


def test_surplus_sell_case_predicted_profit_exceeds_counterfactual_displaced_value():
    """The gate now refuses the sale the closed form said paid +0.44.

    The planner books ``sell_profit`` against c_ref = 0.60 (slot 85), so a
    1.45 SEK/kWh sale looks like +0.44 SEK.  The counterfactual says that
    had the battery held its 2.6 kWh past slot 64, the house would have
    needed to buy it back at 2.55 across slots 68-84 — 6.63 SEK of avoided
    import against 3.77 SEK of revenue, roughly the -3 SEK the reporter
    measured.

    HONEST SCOPE OF THE 6.63-vs-3.77 NUMBER.  It is arithmetic inside this
    test, ``sell_target_kwh * max(buy[65:85])``, NOT a figure the planner
    reports, and it can only fail if the fixture changes.  It is a
    counterfactual: the OFF plan discharges 0.0 kWh into slots 68-84 (it
    uses the stored energy itself) and total grid import is IDENTICAL with
    forced sell ON and OFF (73.45 kWh both ways), so no energy is actually
    displaced between the two runs.  Read the comparison as "this sale paid
    1.45 for energy the evening was paying 2.55 for", not as two measured
    plan outcomes.

    The defect this test stood for is CLOSED: the gate re-plans the tail and
    refuses the window, so the ON run books nothing.  The shape of the
    fixture is pinned below so a later change cannot resurrect the sale.
    """
    data = _surplus_case()
    buy = data["buy"]
    on = _surplus_run(enabled=True)
    off = _surplus_run(enabled=False)

    # Forcing forced sell off removes the sale and its economics entirely.
    assert off.sell_slots == []
    assert off.sell_target_kwh == 0.0
    assert off.sell_revenue == 0.0
    assert off.sell_profit == 0.0
    assert all(s.sell_kwh == 0.0 for s in off.slots)

    # The gate refuses the ON sale too: the closed-form +0.44 was an
    # underestimate of what the energy was worth where it sat.
    assert on.sell_slots == []
    assert on.sell_target_kwh == 0.0
    assert on.sell_revenue == 0.0
    assert on.sell_profit == 0.0
    assert all(s.sell_kwh == 0.0 for s in on.slots)
    assert all(s.action != "force_sell" for s in on.slots)
    assert on.total_profit == pytest.approx(off.total_profit)

    # The gate's own margin, staged from the pre-gate planner: keeping the
    # 2.6 kWh is strictly cheaper than selling it at 1.45.
    with_sale, keeping = _tail_delta(
        buy, data["sell"], data["solar"], _surplus_cfg(),
        0, data["start_soc_pct"],
    )
    assert with_sale > keeping, "keeping must be the cheaper side to refuse"
    assert with_sale - keeping > 0.5

    # The counterfactual is unchanged: the withdrawn energy was worth more
    # at the peak than the sale pays.  Test arithmetic, not a planner output.
    counterfactual_avoided_import = 2.6 * max(buy[65:85])
    assert counterfactual_avoided_import == pytest.approx(2.6 * 2.55)
    assert counterfactual_avoided_import - 2.6 * 1.45 > 0.0

    # No energy is displaced between the two runs: both plans import the
    # same total and neither exports or sells a kWh into the evening peak.
    assert off.grid_import_kwh == pytest.approx(on.grid_import_kwh)
    assert off.grid_import_kwh == pytest.approx(73.45)
    assert all(
        off.slots[s].sell_kwh == 0.0 and off.slots[s].export_kwh == 0.0
        for s in range(65, 85)
    )
    assert all(
        on.slots[s].sell_kwh == 0.0 and on.slots[s].export_kwh == 0.0
        for s in range(65, 85)
    )


def test_surplus_sell_case_bookkeeping_reaches_soc_trajectory():
    """The booked sale must be visible in ``soc_after``.

    Was a strict xfail on the INVERTED assertion: the forced-sell emit block
    (``optimizer.py`` ~2807-2811) mutated ``action``, ``slot_value``,
    ``sell_kwh`` and ``sell_source`` but never rewrote ``soc_after``, so the
    withdrawal left no trace in the trajectory that ``coordinator.py`` ~829
    and ``sensor.py`` ~203/782/812 read.

    Now that ``_correct_soc_for_forced_sell`` rewrites the trajectory forward
    from the first sell slot, the marker is gone and the shape is pinned
    instead of merely detected.  The per-slot arithmetic lives in
    ``test_forced_sell_withdrawal_lands_in_the_soc_trajectory``; this test
    keeps the coarse "the trajectory moved" statement the xfail stood for.

    Re-anchored to the cheap-surplus fixture: the full-day fixture's sale is
    now refused by the gate, so it has no withdrawal to book.  The sale the
    gate KEEPS still has to leave a trace in the trajectory.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35))
    on = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    off = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0,
                       enable_forced_sell=False)
    assert on.sell_slots, "the gate must keep this sale for the test to run"
    first = on.sell_slots[0]
    assert on.slots[first].action == "force_sell"
    assert on.slots[first].sell_kwh > 0.0
    assert [round(s.soc_after, 9) for s in on.slots] != [
        round(s.soc_after, 9) for s in off.slots
    ], (
        f"forced sell withdrew {on.sell_target_kwh} kWh but soc_after is "
        "identical ON vs OFF"
    )


# ---------------------------------------------------------------------------
# A2 — the withdrawal lands in the trajectory, slot by slot.
# ---------------------------------------------------------------------------


def test_forced_sell_withdrawal_lands_in_the_soc_trajectory():
    """The shift is exactly the energy withdrawn, accumulated from slot one.

    Strengthens the former xfail: not just that the trajectory moves, but that
    from the first sell slot onward it sits ``cumulative_withdrawn`` below the
    same run with forced sell off, and that nothing before the window moved.

    Re-anchored to the cheap-surplus fixture (the full-day fixture's sale is
    now refused, so it has no withdrawal to trace).
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35))
    capacity = cfg.capacity_kwh
    on = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    off = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0,
                       enable_forced_sell=False)
    assert on.sell_slots, "fixture must still produce a sale"
    first = on.sell_slots[0]

    assert all(
        on.slots[s].soc_after == off.slots[s].soc_after
        for s in range(first)
    ), "the withdrawal must not move slots before the window"
    withdrawn = 0.0
    for s in range(first, N):
        withdrawn += on.slots[s].sell_kwh
        assert on.slots[s].soc_after == pytest.approx(
            off.slots[s].soc_after - withdrawn / capacity * 100.0, abs=1e-9
        ), f"slot {s}: trajectory does not reflect {withdrawn} kWh withdrawn"


@pytest.mark.parametrize("start_slot", [0, 63])
@pytest.mark.parametrize("start_soc_pct", [20.0, 37.0, 51.0, 80.0, 100.0])
def test_post_sale_soc_is_always_physically_possible(start_slot, start_soc_pct):
    """No entry may plan a withdrawal that breaks the SoC envelope.

    Asserts the invariant rather than one arithmetic path, so it holds for
    every entry the sweep covers: the rewritten trajectory stays inside
    ``[0, soc_max]`` everywhere, and the battery is still at or above
    ``soc_min`` on every slot the plan actually sells from.
    """
    data = _surplus_case()
    cfg = _surplus_cfg()
    res = opt.optimize(
        data["buy"], data["sell"], data["solar"], cfg,
        initial_soc_pct=start_soc_pct, start_slot=start_slot,
    )
    assert all(0.0 <= sp.soc_after <= cfg.soc_max for sp in res.slots)
    if res.sell_slots:
        assert min(res.slots[s].soc_after for s in res.sell_slots) >= cfg.soc_min


# ---------------------------------------------------------------------------
# A1 — the sell budget is read AT the sell slot, not at the window start.
# ---------------------------------------------------------------------------


def _baseline_with_soc(soc_at_window, soc_before=80.0, action="idle"):
    """96-slot plan whose ``soc_after`` encodes a charged or drained battery.

    ``soc_before`` is the SoC at the start of the planning window, the window
    slots (34-37) carry ``soc_at_window`` — i.e. the plan moved between the
    two.  Both readings matter: the sell budget used to be a single
    start-of-window number, which is wrong in both directions.
    """
    return [
        opt.SlotPlan(
            index=s, action=action, slot_value=opt.SLOT_IDLE,
            buy_price=0.03, sell_price=0.20 if s in WINDOW else 0.08,
            soc_after=soc_at_window if s in WINDOW else soc_before,
        )
        for s in range(N)
    ]


def test_sell_budget_is_read_at_the_sell_slot_when_the_plan_drained():
    """A plan that DRAINED must not sell start-of-window energy.

    SoC 80 % before the window, 40 % at it.  Budgeting from the start-of-window
    figure allows 8 - 2 (soc_min) - 1 (reserve) = 5.0 kWh and stages the full
    2.5 kWh per-slot ceiling in four slots, while the battery only holds
    4 - 2 - 1 = 1.0 kWh sellable at slot 34 and nothing once that is gone.
    """
    cfg = _cfg(budget=0.0, capacity=10.0)
    buy, sell, _solar = _prices()
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, _baseline_with_soc(40.0, soc_before=80.0),
        start_slot=0, round_trip_factor=ETA_RT,
    )
    assert plan == {34: (1.0, "battery")}


def test_sell_budget_is_read_at_the_sell_slot_when_the_plan_charged():
    """The mirror image: a plan that CHARGED may sell more than start-of-window.

    30 % before the window, 80 % at it.  The start-of-window budget was
    3 - 2 - 1 = 0 kWh, so the planner refused to sell anything although the
    battery is charged.  Read at the slot it is 8 - 2 - 1 = 5.0 kWh, which
    fills the two full per-slot ceilings the window can hold before the third
    slot has nothing left.
    """
    cfg = _cfg(budget=0.0, capacity=10.0)
    buy, sell, _solar = _prices()
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, _baseline_with_soc(80.0, soc_before=30.0),
        start_slot=0, round_trip_factor=ETA_RT,
    )
    assert list(plan) == [34, 35]
    assert [kwh for kwh, _tier in plan.values()] == [2.5, 2.5]


# ---------------------------------------------------------------------------
# A4 — the field case entered mid-day, already spent.
# ---------------------------------------------------------------------------


def _midday_case():
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "tests" / "fixtures" / "surplus_sell_case_midday.json"
    )
    return json.loads(path.read_text())


def _midday_cfg(enabled=True):
    data = _midday_case()
    return opt.BatteryConfig(
        forced_sell_enabled=enabled,
        forced_sell_max_kwh_pd=6.0,
        forced_sell_min_profit=0.02,
        forced_sell_min_price=0.0,
        **data["cfg"],
    )


def _midday_run(enabled=True, soc=None):
    data = _midday_case()
    return opt.optimize(
        data["buy"], data["sell"], data["solar"], _midday_cfg(enabled),
        initial_soc_pct=data["start_soc_pct"] if soc is None else soc,
        start_slot=data["start_slot"],
    )


def test_midday_fixture_enters_at_the_field_slot_and_soc():
    """The fixture models 15:45 at 37 %, not midnight at 51 %.

    surplus_sell_case.json buys at 0.25-0.60 across slots 0-62 and therefore
    CHARGES to 84.5 % before the sell window: it exports cheaply-charged
    surplus, where the sale really is correct.  Same prices, different entry.
    """
    data = _midday_case()
    full = _surplus_case()
    assert data["start_slot"] == 63           # 15:45
    assert data["start_soc_pct"] == 37.0
    assert data["buy"] == full["buy"]
    assert data["sell"] == full["sell"]
    assert data["solar"] == full["solar"]
    assert data["cfg"] == full["cfg"]
    # ... and the full-day fixture really does charge past the entry SoC.
    full_day = _surplus_run()
    assert full_day.slots[62].soc_after > 80.0


def test_midday_case_reproduces_the_defect_shape():
    """The gate refuses the mid-day sale the evening peak outbids.

    Same shape as the full-day fixture and the same numbers here: the sell
    slot sits at 1.45 while the day max is 2.30, and the cheapest forward buy
    from the sell slot is at slot 85, AFTER the 2.55 peak at slots 68-84.  What
    the mid-day entry adds is that the battery is already spent, so the sale
    takes energy the evening was about to use -- and the gate now says no.

    HONEST SCOPE: the displaced-value figure below is arithmetic inside this
    test (``withdrawn * peak``), NOT a planner output, exactly as in
    test_surplus_sell_case_predicted_profit_exceeds_counterfactual_displaced_value.
    It can only fail if the fixture changes.
    """
    data = _midday_case()
    buy = data["buy"]
    res = _midday_run()
    # The gate refuses: the nomination is real but the tail re-plan says
    # keeping the energy is cheaper.
    assert res.sell_slots == []
    assert res.sell_target_kwh == 0.0
    assert res.sell_revenue == 0.0
    assert res.sell_profit == 0.0
    assert all(s.action != "force_sell" for s in res.slots)

    # The nomination the gate rejected: a 1.45 sale whose cheapest forward
    # buy (0.60 at slot 85) sits AFTER the 2.55 peak.
    cfg_on = _midday_cfg()
    plan = opt._plan_forced_sell_slots(
        cfg_on, data["buy"], data["sell"],
        _midday_run(enabled=False).slots, data["start_slot"],
        cfg_on.round_trip_factor, total_slots=opt.SLOTS_PER_DAY,
    )
    assert plan, "the nomination must still fire for the gate to refuse"
    first = max(plan)
    assert first >= data["start_slot"]
    assert data["sell"][first] < max(data["sell"])
    cheapest = min(range(first + 1, N), key=lambda c: buy[c])
    assert cheapest == 85
    assert cheapest > 84, "argmin must land after the evening peak"

    # The gate's own margin: keeping beats selling.
    with_sale, keeping = _tail_delta(
        data["buy"], data["sell"], data["solar"], cfg_on,
        data["start_slot"], data["start_soc_pct"],
    )
    assert with_sale > keeping, "keeping must be the cheaper side to refuse"
    assert with_sale - keeping > 0.5

    # The counterfactual is unchanged: the withdrawn energy was worth more
    # at the peak than the sale pays.  Test arithmetic, not a planner output.
    withdrawn = sum(k for k, _ in plan.values())
    displaced = withdrawn * max(buy[first + 1 : cheapest + 1])
    assert displaced > withdrawn * data["sell"][first]


def test_midday_case_budget_is_read_at_the_second_sell_slot():
    """The second sell slot cannot re-spend what the first one withdrew.

    At 51 % the trajectory reads 50.75 % at slot 63 and 50.50 % at slot 64.
    Budget read at the slot: 5.075 - 1.5 (soc_min) - 1.0 (reserve) = 2.575 at
    the first slot, and 5.05 - 1.3 (already withdrawn) - 1.5 - 1.0 = 1.25 at
    the second — which is what caps it below the 1.3 kWh per-slot ceiling.
    The old start-of-window budget offered 2.6 kWh of headroom and staged
    1.3 in both slots.

    The nomination is unchanged -- 1.3 then 1.25 -- but the gate refuses the
    window, so nothing is staged.  The per-slot budget arithmetic is pinned
    on the nomination, not on the result.
    """
    data = _midday_case()
    cfg_on = _midday_cfg()
    base = opt.optimize(
        data["buy"], data["sell"], data["solar"], _midday_cfg(enabled=False),
        initial_soc_pct=51.0, start_slot=data["start_slot"],
    )
    plan = opt._plan_forced_sell_slots(
        cfg_on, data["buy"], data["sell"],
        base.slots, data["start_slot"], cfg_on.round_trip_factor,
        total_slots=opt.SLOTS_PER_DAY,
    )
    assert sorted(plan) == [63, 64]
    assert plan[63][0] == pytest.approx(1.3)
    assert plan[64][0] == pytest.approx(1.25)
    assert sum(k for k, _ in plan.values()) == pytest.approx(2.55)

    res = _midday_run(soc=51.0)
    assert res.sell_slots == []
    assert res.sell_target_kwh == 0.0
def _empty_then_charged_case():
    """Arrays for the empty-then-charged window regression.

    15 kWh pack, ``soc_min`` 20 %, entering the day at 23.6 %.  Buy is
    expensive (0.20) everywhere except slots 26-38 at 0.06, so the pack
    drains toward the floor through the morning and only charges on the
    cheap block.  Sell is a flat 0.30 band with 0.90 peaks at slots 5, 6,
    40 and 41.

    The round-trip gate passes from slot 0 onward (0.30 or 0.90 against a
    0.06 forward buy), so the *only* thing standing between this plan and a
    sale is the per-slot energy guard.
    """
    return _prices(
        peak_slots=[5, 6, 40, 41],
        peak=0.90,
        flat_sell=0.30,
        flat_buy=0.20,
        buy_block=(26, 39, 0.06),
    )


def _empty_then_charged_cfg(enabled=True):
    """15 kWh / 20 % floor / 1.5 kWh per-slot ceiling / 6 kWh daily budget."""
    cfg = _cfg(
        budget=6.0,
        capacity=15.0,
        max_discharge_kw=6.0,
        max_charge_kw=6.0,
    )
    cfg.forced_sell_enabled = enabled
    return cfg


def test_nothing_sellable_at_one_slot_does_not_end_the_window_search():
    """An empty passing-price slot must not abandon the whole day.

    Regression for the unconditional ``break`` on the per-slot energy guard
    in ``_plan_forced_sell_slots``.  "Nothing sellable HERE" is not a
    terminal condition: the plan may charge later in the day, and the
    window should legitimately open at a later, still-passing slot.

    The pre-existing ``if remaining <= 0.05: break`` was safe because
    ``remaining`` decreased monotonically.  ``available_kwh`` does not — it
    rises while the plan charges — so the same control flow is wrong, and it
    is wrong in the expensive direction: the whole day's sale is lost.

    Premise, asserted from the forced-sell-off run so the test states it
    rather than assumes it:

    * slot 5 passes the price and profit gates, but the trajectory holds
      3.39 kWh against a 3.0 kWh floor plus a 1.0 kWh reserve -> 0.0
      sellable.  The window cannot open here.
    * the plan then charges across slots 26-38, so by slot 26 it holds
      5.10 kWh -> 1.1 kWh sellable.  The window opens HERE.
    * by slot 30 the pack is 71.3 % and by slot 40 it is 98.2 %.

    Before the fix the guard at slot 5 broke out of the loop and the day
    came back with no sale at all — an empty ``sell_slots``, target 0.0.
    """
    buy, sell, solar = _empty_then_charged_case()
    cfg = _empty_then_charged_cfg()

    base = opt.optimize(
        buy, sell, solar, _empty_then_charged_cfg(enabled=False),
        initial_soc_pct=23.6,
    )
    # The gate really does pass at slot 5 — this is an energy problem, not
    # a price problem.
    c_ref_5 = min(buy[6:])
    assert sell[5] >= cfg.forced_sell_min_price
    assert (sell[5] - WEAR) * ETA_RT - c_ref_5 >= cfg.forced_sell_min_profit
    held_5 = base.slots[5].soc_after * 15.0 / 100.0
    assert held_5 - 3.0 - 1.0 <= 0.05, "slot 5 must have nothing sellable"
    # ... and the cheap block really does make it sellable by slot 26.
    held_26 = base.slots[26].soc_after * 15.0 / 100.0
    assert held_26 - 3.0 - 1.0 > 0.05, "slot 26 must be sellable"

    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=23.6)

    # The window opens at the first slot the battery can actually back, not
    # at the first slot that merely passes the price gate.
    assert res.sell_slots == [26, 27, 28, 29, 30]
    assert 5 not in res.sell_slots and 6 not in res.sell_slots
    assert res.sell_target_kwh == pytest.approx(6.0)
    # Gate margin (cost_keeping - cost_selling), including the wear the six sales
    # cause: the 0.06 buy block makes the tail's displaced energy worth 1.26
    # SEK against the 1.80 of revenue.
    assert res.sell_profit == pytest.approx(1.26)

    # Per slot: min(1.5 per-slot ceiling, held - floor - reserve - already
    # withdrawn, daily budget left).  Slot 26 can only give 1.1; slots 27-29
    # give 1.4 each; slot 30 takes the 0.7 that finishes the 6 kWh budget.
    assert res.slots[26].sell_kwh == pytest.approx(1.1)
    assert res.slots[27].sell_kwh == pytest.approx(1.4)
    assert res.slots[28].sell_kwh == pytest.approx(1.4)
    assert res.slots[29].sell_kwh == pytest.approx(1.4)
    assert res.slots[30].sell_kwh == pytest.approx(0.7)
    for s in res.sell_slots:
        assert res.slots[s].sell_source == "battery"


def test_open_window_still_stops_when_the_battery_empties():
    """The counterpart guard: once the window is open, running dry ends it.

    ``available_kwh`` is not monotonic, so the guard has to distinguish the
    two cases the unconditional ``break`` conflated.  Pre-window "nothing
    here yet" must keep scanning; mid-window "the window just emptied the
    pack" must stop, so the sale does not resume after a hole.

    Same arrays, forced sell already opened at slot 26 by the first test's
    plan; raising the starting SoC shifts the opening slot earlier, which
    puts an empty slot inside the live window.
    """
    buy, sell, solar = _empty_then_charged_case()
    cfg = _empty_then_charged_cfg()

    # Start well above the floor: the window opens immediately, the pack is
    # then drained by the greedy plan, and the guard must stop the sale
    # rather than resume it further along the day.
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.sell_slots, "a full battery must still sell"
    # A resumed-after-a-hole window would show a gap in the middle.
    assert res.sell_slots == list(range(res.sell_slots[0], res.sell_slots[-1] + 1))
# ---------------------------------------------------------------------------
# Task 3b -- the tail re-plan gate.
#
# ``_plan_forced_sell_slots`` prices a candidate slot against ``c_ref``, the
# CHEAPEST buy over the whole remaining day.  That ignores any dearer load
# period that intervenes before the cheap buy, so it sells into a slot the
# evening is about to outbid.  The gate re-plans the tail of the day from
# the SoC the sale leaves behind and asks the only question that matters:
# is the energy worth more where it is, than sold here?
# ---------------------------------------------------------------------------


def _tail_delta(buy, sell, solar, cfg, start_slot, start_soc):
    """The gate's own arithmetic, recomputed WITHOUT re-applying the gate.

    Returns ``(cost_with_sale, cost_keeping)`` for the tail that follows the
    sell window.  ``cost_with_sale`` is that tail re-planned from the
    post-sale SoC, less the revenue the sale brings; ``cost_keeping`` is the
    SAME tail re-planned from the SoC the battery held before the window.
    The sale is kept when ``cost_with_sale < cost_keeping``.

    Staged from the PRE-GATE planner: run the ordinary plan (forced sell off)
    to get the trajectory, then nominate the window with
    ``_plan_forced_sell_slots`` -- the same first pass the gate runs -- and
    price the two tails with ``_forced_sell_tail_cost``.  Calling
    ``optimize()`` here would re-apply the gate and loop.
    """
    plan = opt.optimize(
        buy, sell, solar, cfg,
        initial_soc_pct=start_soc, start_slot=start_slot,
        enable_forced_sell=False,
    )
    result_slots = plan.slots
    sell_plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, result_slots, start_slot, cfg.round_trip_factor,
        total_slots=opt.SLOTS_PER_DAY,
    )
    assert sell_plan, "no sale staged; the gate has nothing to price"
    p = max(sell_plan) + 1
    withdrawn = sum(kwh for kwh, _tier in sell_plan.values())
    revenue = sum(sell[s] * kwh for s, (kwh, _tier) in sell_plan.items())
    pp = 100.0 / cfg.capacity_kwh
    pre_pct = result_slots[p - 1].soc_after
    passthrough = dict(
        enable_pv_strategy=False, emaldo_modes=None, case_a_floor=None,
        solar_regime_engaged=False, future_min_buy=None,
        future_grid_charge_needed=None, solar_confidence_ratio=None,
        total_slots=opt.SLOTS_PER_DAY,
    )
    keeping = opt._forced_sell_tail_cost(
        cfg, buy, sell, solar, p, pre_pct, **passthrough)
    with_sale = opt._forced_sell_tail_cost(
        cfg, buy, sell, solar, p, pre_pct - withdrawn * pp,
        **passthrough) - revenue
    return with_sale, keeping


def test_midday_forced_sell_is_refused_when_the_tail_buys_it_back_dearer():
    """Issue #23: the field case must stop selling into the evening peak.

    ``surplus_sell_case_midday.json`` is the reporter's day -- 15:45 entry at
    37 %, a 1.45 SEK/kWh sale at slot 63, a 2.55 SEK/kWh peak at slots
    68-84, and a 0.60 reference buy at slot 85 that ``c_ref`` reads as "the
    price you will pay back".  The tail re-plan prices the actual
    alternative: keeping the 1.175 kWh costs more than the 1.70 SEK the
    sale brings, so the whole window is refused.
    """
    data = _midday_case()
    res = _midday_run()

    # The gate's own comparison, on the plan the fixture still stages today.
    with_sale, keeping = _tail_delta(
        data["buy"], data["sell"], data["solar"], _midday_cfg(),
        data["start_slot"], data["start_soc_pct"],
    )
    assert with_sale > keeping, (
        "keeping the energy has to be the cheaper side for this to refuse"
    )
    assert with_sale - keeping > 0.5, "margin must not be a rounding artefact"

    assert res.sell_slots == []
    assert res.sell_target_kwh == 0.0
    assert res.sell_revenue == 0.0
    assert res.sell_profit == 0.0
    assert all(s.sell_kwh == 0.0 for s in res.slots)
    assert all(s.action != "force_sell" for s in res.slots)


def test_midday_refusal_is_driven_by_the_tail_and_not_by_the_entry_slot():
    """The same entry still sells once the evening peak it was skipping is gone.

    The only change from the fixture above is the price of slots 65-84 --
    the load the sale was beating instead of a reference buy.  With the peak
    flattened to 0.60 there is nothing dearer left to displace, keeping the
    energy buys nothing back, and the same window is kept.  This is the
    discriminator against a gate that simply switches the feature off, and
    against one that keys on ``start_slot``.
    """
    data = _midday_case()
    buy = list(data["buy"])
    for s in range(64, 85):
        buy[s] = 0.60
    res = opt.optimize(
        buy, data["sell"], data["solar"], _midday_cfg(),
        initial_soc_pct=data["start_soc_pct"], start_slot=data["start_slot"],
    )
    assert res.sell_slots, "flattening the tail peak must restore the sale"
    assert res.sell_slots[0] == data["start_slot"]
    assert res.sell_target_kwh > 0.0
    assert res.sell_revenue > 0.0

    with_sale, keeping = _tail_delta(
        buy, data["sell"], data["solar"], _midday_cfg(),
        data["start_slot"], data["start_soc_pct"],
    )
    assert with_sale < keeping, (
        "the flattened tail must make selling the cheaper side"
    )


def test_the_gate_leaves_the_cheap_surplus_sale_alone():
    """The feature must still fire: a sale nothing can outbid is kept.

    Buys flat at the wear floor, so the tail has no discharge and no
    recharge worth having -- the energy has no better home than the grid at
    0.20 SEK/kWh.  Same window, same window slots, same revenue: only the
    tail re-plan decides this one.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35))
    res = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)

    with_sale, keeping = _tail_delta(buy, sell, solar, cfg, 0, 100.0)
    assert with_sale < keeping, (
        "the tail must make selling the cheaper side for the gate to keep it"
    )
    assert res.sell_slots == [34, 35]
    assert res.sell_target_kwh == pytest.approx(5.0)
    assert res.sell_revenue == pytest.approx(1.0)
    assert res.sell_profit > 0.0


def test_enable_forced_sell_false_suppresses_the_window():
    """The re-plan needs a switch that is independent of ``_probe``.

    Both re-plans inside the gate run with ``enable_forced_sell=False`` and
    ``_probe=True`` so neither can stage a sale of its own and start
    recursing.  ``_probe`` alone already does that; the explicit flag is
    what keeps the intent readable and survives a change to the probe guard.
    """
    cfg = _cfg(budget=6.0, capacity=15.0)
    buy, sell, solar = _prices(peak_slots=(34, 35))
    res = opt.optimize(
        buy, sell, solar, cfg,
        initial_soc_pct=100.0, enable_forced_sell=False,
    )
    assert res.sell_slots == []
    assert res.sell_target_kwh == 0.0
    # Suppressing the window is not the gate refusing: the plan itself is
    # unchanged, nothing downstream moved.
    on = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    assert res.total_profit == pytest.approx(on.total_profit)
    assert res.grid_import_kwh == pytest.approx(on.grid_import_kwh)
    # Enabling forced sell must not move any slot OUTSIDE the sale window: the
    # plan is identical everywhere the window does not reach.  Comparing the
    # whole action list failed the moment the window gained force_sell actions.
    outside = [s for s in range(len(res.slots)) if s not in set(on.sell_slots)]
    assert [res.slots[s].action for s in outside] == [
        on.slots[s].action for s in outside
    ]


def test_tail_replan_does_not_deepen_planner_recursion(monkeypatch):
    """The gate adds planner runs but must not add a recursion level.

    ``_probe`` is the only guard in the planner and the gate's re-plans pass
    it, so they cannot reach ``_find_plateau_edge`` again.  Measured against
    the same run with forced sell disabled -- the depth the planner reached
    before this change.
    """
    import dataclasses

    data = _midday_case()

    def max_depth(cfg):
        state = {"now": 0, "max": 0}
        real = opt.optimize

        def counting(*a, **k):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            try:
                return real(*a, **k)
            finally:
                state["now"] -= 1

        monkeypatch.setattr(opt, "optimize", counting)
        try:
            opt.optimize(
                data["buy"], data["sell"], data["solar"], cfg,
                initial_soc_pct=data["start_soc_pct"],
                start_slot=data["start_slot"],
            )
        finally:
            monkeypatch.setattr(opt, "optimize", real)
        return state["max"]

    baseline = max_depth(
        dataclasses.replace(_midday_cfg(), forced_sell_enabled=False)
    )
    with_gate = max_depth(_midday_cfg())
    assert baseline >= 1
    assert with_gate == baseline, (
        f"gate deepened recursion: {baseline} -> {with_gate}"
    )


# ---------------------------------------------------------------------------
# D2 — staged target rounding. Issue #23 comment 6065753666.
# ---------------------------------------------------------------------------


def test_stage_sell_target_floors_and_never_rounds_up():
    """1.55 kWh of plan staged as 2 kWh overshot the SoC floor.

    Emaldo's ``EmaldoManualSellingSwitch._current_target()`` applies
    ``int(round(...))`` to whatever is staged, so the staged number is an
    upper bound on the export, never a lower one. Staging 1.55 became 2,
    the device exported at 9 kW until SoC reached 13 percent against a 15
    percent floor, and the next run bought ~1 kWh back at 2.43 SEK/kWh.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import load_coordinator

    stage = load_coordinator()[0]._stage_sell_target_kwh
    assert stage(1.55) == 1.0
    # An already-whole number rounds to itself, including one that
    # arrived as 1.9999999999999998 from float arithmetic.
    assert stage(2.0) == 2.0
    assert stage(1.0) == 1.0
    assert stage(1.9999999999999998) == 2.0
    assert stage(0.0) == 0.0
    assert stage(-1.0) == 0.0


def test_stage_sell_target_refuses_sub_kwh_rather_than_rounding_up():
    """The opposite error, guarded: max(1, floor(x)) would overshoot.

    At 0.6 kWh of plan that stages 1 kWh and overshoots by 0.4. A
    sub-kWh remainder is therefore refused, and the caller opens no sale.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import load_coordinator

    stage = load_coordinator()[0]._stage_sell_target_kwh
    assert stage(0.6) == 0.0
    assert stage(0.4) == 0.0
    assert stage(0.999999) == 0.0


def test_battery_tier_gate_uses_exact_wear_form():
    """The round-trip gate prices wear at its full cost, not scaled by the
    round-trip factor.

    The previous form ``(sell - wear) * round_trip_factor - c_ref`` multiplied
    wear by `round_trip_factor` (0.9025) and so under-penalised it by about
    19%, nominating cycles that lose money on wear. The exact condition for a
    charge-then-discharge cycle to pay is
    ``sell * round_trip_factor > buy + wear``.

    Flat sell 0.08, reference buy 0.024, wear 0.03, min_profit 0.02:

      exact  0.08 * 0.9025 - 0.03 - 0.024 = 0.0182 < 0.02  -> refuse
      old    (0.08 - 0.03) * 0.9025 - 0.024 = 0.0211 >= 0.02 -> nominate

    So the flat slots are refused and only the 0.20 peak at slot 34 is sold.
    """
    cfg = opt.BatteryConfig(
        forced_sell_enabled=True,
        forced_sell_max_kwh_pd=6.0,
        forced_sell_min_profit=0.02,
        forced_sell_min_price=0.0,
        capacity_kwh=15.0,
        max_discharge_kw=10.0,
        max_charge_kw=10.0,
        wear_cost_per_kwh=0.03,
    )
    buy = [0.03] * N
    buy[35] = 0.024
    for s in range(36, N):
        buy[s] = 0.30
    sell = [0.08] * N
    sell[34] = 0.20
    solar = [0.0] * N
    baseline = opt.optimize(buy, sell, solar, cfg, initial_soc_pct=100.0)
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, baseline.slots,
        start_slot=0, round_trip_factor=ETA_RT,
    )
    # The 0.20 peak is profitable under either wear form and is sold.
    assert 34 in plan
    assert plan[34] == (2.5, "battery")
    # The flat 0.08 slots lose money once wear is priced at its full cost;
    # the old scaled-wear form would have nominated them.
    assert 0 not in plan
    assert 1 not in plan
    # The baseline itself must not sell flat energy it cannot profit from.
    assert baseline.sell_slots == [34]
    assert baseline.sell_target_kwh == pytest.approx(2.5)