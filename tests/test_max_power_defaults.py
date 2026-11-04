"""Per-slot energy is power x slot duration, in both forced-sell tiers.

A 15-minute slot is 0.25 h, so x kW sustained for the slot moves x * 0.25 kWh.
Both forced-sell per-slot ceilings used to be pinned to a literal 2.5 kWh, and
the ``max_charge_kw`` / ``max_discharge_kw`` fields carried 2.5 in a *kW*
position -- a per-slot kWh number in a power field -- so the user's configured
inverter power was ignored. This file pins the derivation:

* the declared defaults (10.0 kW) reproduce exactly 2.5 kWh per slot, so a
  user on the defaults sees no behaviour change at all;
* a non-default power is honoured by the matching per-slot property;
* the coordinator's config fallback reads the declared constants;
* ``_plan_forced_sell_charge`` (a grid charge) scales with ``max_charge_kw``;
* ``_plan_forced_sell_slots`` (a grid discharge) still scales with
  ``max_discharge_kw`` -- regression guard for db78f8a.

Assumption under test: Emaldo's grid power limit is NOT set, so the inverter
runs at the full configured power for the whole slot and the derived figure is
the real per-slot energy. With a grid power limit set, both ceilings would have
to be clamped lower; nothing in this integration can see that limit today.
"""
from __future__ import annotations

from types import SimpleNamespace

from . import load_coordinator, load_optimizer

opt, const = load_optimizer()
coord, _coord_const = load_coordinator()

N = opt.SLOTS_PER_DAY
ETA_RT = 0.9025                # charge_efficiency * discharge_efficiency
SLOT_H = opt.SLOT_DURATION_HOURS
BUY = 0.03                     # flat buy price (sell gate fails below the peak)
SELL_PEAK = 0.20               # sell price in the window
SELL_FLAT = 0.06               # sell price outside the window
WINDOW = (34, 35)


def _cfg(max_charge_kw=None, max_discharge_kw=None, capacity=15.0,
         budget=6.0):
    """Forced-sell config; power fields left at the dataclass default."""
    kwargs = dict(
        forced_sell_enabled=True,
        forced_sell_max_kwh_pd=budget,
        forced_sell_min_profit=0.02,
        forced_sell_min_price=0.0,
        capacity_kwh=capacity,
    )
    if max_charge_kw is not None:
        kwargs["max_charge_kw"] = max_charge_kw
    if max_discharge_kw is not None:
        kwargs["max_discharge_kw"] = max_discharge_kw
    return opt.BatteryConfig(**kwargs)


def _prices():
    buy = [BUY] * N
    sell = [SELL_FLAT] * N
    for s in WINDOW:
        sell[s] = SELL_PEAK
    return buy, sell


def _idle_baseline():
    """96-slot all-idle plan: no Case-A discharge, so the full cap is free.

    ``soc_after`` is filled at 100 % so the forced-sell budget — which reads
    the plan's own trajectory, not a start-of-window figure — has a charged
    battery to size against and never becomes the binding limit in these
    power-scaling tests.
    """
    _buy, sell = _prices()
    return [
        opt.SlotPlan(
            index=s, action="idle", slot_value=opt.SLOT_IDLE,
            buy_price=BUY, sell_price=sell[s], soc_after=100.0,
        )
        for s in range(N)
    ]


def _sell_step(cfg):
    """kWh the sell planner stages in the window's first slot."""
    buy, sell = _prices()
    plan = opt._plan_forced_sell_slots(
        cfg, buy, sell, _idle_baseline(),
        start_slot=0, round_trip_factor=ETA_RT,
    )
    return plan[WINDOW[0]][0]


def _charge_step(cfg, sell_plan, start_soc_kwh):
    """{slot: kwh} the pre-window grid-charge planner returns."""
    buy, sell = _prices()
    return opt._plan_forced_sell_charge(
        cfg, buy, sell, sell_plan,
        start_slot=0, start_soc_kwh=start_soc_kwh, round_trip_factor=ETA_RT,
    )


# ── The declared defaults: no behaviour change ─────────────────────────
#
# Every literal replaced by this fix read 2.5 kWh. With DEFAULT_MAX_*_KW at
# 10.0 the derivation lands on 10.0 * 0.25 = 2.5, so a user running the
# declared defaults gets byte-identical sizing.


def test_declared_default_power_is_ten_kw():
    """Guard the premise: both defaults are 10.0 kW and the slot is 0.25 h."""
    assert const.DEFAULT_MAX_CHARGE_KW == 10.0
    assert const.DEFAULT_MAX_DISCHARGE_KW == 10.0
    assert SLOT_H == 0.25


def test_dataclass_defaults_match_declared_constants():
    """A bare BatteryConfig carries the declared power, not 2.5 kW.

    2.5 is a per-slot kWh figure; it must never appear in a kW field.
    """
    cfg = opt.BatteryConfig()
    assert cfg.max_charge_kw == const.DEFAULT_MAX_CHARGE_KW
    assert cfg.max_discharge_kw == const.DEFAULT_MAX_DISCHARGE_KW


def test_declared_defaults_give_2_5_kwh_per_slot_both_tiers():
    """The no-change proof, on both sides.

    Charge and discharge per-slot energy are both exactly 2.5 kWh -- the
    number every replaced literal held.
    """
    cfg = opt.BatteryConfig()
    assert cfg.max_charge_per_slot_kwh == 2.5
    assert cfg.max_discharge_per_slot_kwh == 2.5


def test_default_power_planners_stage_2_5_kwh():
    """Both planners stage 2.5 kWh at the declared default power.

    Ties the property maths to what the forced-sell planners actually emit,
    on both the charge and the discharge side.
    """
    cfg = _cfg()
    assert _sell_step(cfg) == 2.5
    # required_kwh is min(battery 5.0, headroom 6.0, budget 6.0) = 5.0, so the
    # 2.5 per-slot charge ceiling is the binding term.
    plan = _charge_step(cfg, {34: (2.5, "battery"), 35: (2.5, "battery")}, 9.0)
    assert plan == {0: 2.5}


# ── Non-default power is honoured, in kW, on the matching side ─────────


def test_per_slot_properties_track_configured_power():
    """Each per-slot property follows its own power field, nothing else."""
    for kw in (2.5, 4.0, 5.0, 10.0, 12.0, 20.0):
        cfg = opt.BatteryConfig(max_charge_kw=kw, max_discharge_kw=kw)
        assert cfg.max_charge_per_slot_kwh == kw * SLOT_H
        assert cfg.max_discharge_per_slot_kwh == kw * SLOT_H
    # The two sides are independent.
    mixed = opt.BatteryConfig(max_charge_kw=4.0, max_discharge_kw=6.0)
    assert mixed.max_charge_per_slot_kwh == 1.0
    assert mixed.max_discharge_per_slot_kwh == 1.5


def test_charge_step_scales_with_max_charge_kw():
    """The pre-window grid charge is sized from the charge power.

    required_kwh is 5.0 in every case below (battery need 5.0, headroom 6.0,
    budget 6.0), so the per-slot ceiling is what binds -- until the ceiling
    exceeds the requirement and required_kwh takes over at 20.0 kW.
    """
    sell_plan = {34: (2.5, "battery"), 35: (2.5, "battery")}
    for kw, expected in ((2.5, 0.625), (4.0, 1.0), (8.0, 2.0), (10.0, 2.5)):
        cfg = _cfg(max_charge_kw=kw)
        assert cfg.max_charge_per_slot_kwh == kw * SLOT_H
        assert _charge_step(cfg, sell_plan, 9.0) == {0: expected}
    # 20.0 kW -> 5.0 kWh per slot, above the 5.0 kWh required: still 5.0.
    assert _charge_step(_cfg(max_charge_kw=20.0), sell_plan, 9.0) == {0: 5.0}


def test_sell_cap_scales_with_max_discharge_kw():
    """Regression guard for db78f8a: the sell ceiling follows discharge power.

    10.0 kW -> 2.5 kWh, 5.0 kW -> 1.25 kWh, 20.0 kW -> 5.0 kWh. The discharge
    power must not leak into the charge side, or vice versa.
    """
    for kw, expected in ((2.5, 0.625), (5.0, 1.25), (10.0, 2.5), (20.0, 5.0)):
        assert _sell_step(_cfg(max_discharge_kw=kw)) == expected
        # Charge power is irrelevant to the sell ceiling.
        assert _sell_step(_cfg(max_discharge_kw=kw, max_charge_kw=3.0)) == expected


def test_charge_and_discharge_tiers_read_different_powers():
    """Each tier is sized from its own field when the two disagree.

    Low charge power / high discharge power: the grid charge shrinks to 1.0 kWh
    while the sell ceiling stays at 5.0 kWh. A single shared "max power" would
    get at least one of the two wrong.
    """
    cfg = _cfg(max_charge_kw=4.0, max_discharge_kw=20.0)
    assert _sell_step(cfg) == 5.0
    assert _charge_step(cfg, {34: (2.5, "battery"), 35: (2.5, "battery")}, 9.0) \
        == {0: 1.0}


# ── Coordinator fallback: the config-absent path uses the constants ────


def _coord_with(config):
    """A coordinator with only the state ``_build_battery_config`` reads.

    ``config`` is a read-only property merging ``_entry.data`` and
    ``_entry.options``, so the entry is what has to be stubbed.
    """
    coord_obj = object.__new__(coord.BatteryOptimizerCoordinator)
    coord_obj._auto_base_load_value = None
    coord_obj._entry = SimpleNamespace(data=dict(config), options={})
    return coord_obj


def test_coordinator_fallback_uses_declared_power_constants():
    """With no configured power, the coordinator builds 10.0 kW / 2.5 kWh.

    The fallback previously read 2.5 straight into a kW field, which is the
    same units bug one layer up.
    """
    bc = _coord_with({})._build_battery_config()
    assert bc.max_charge_kw == const.DEFAULT_MAX_CHARGE_KW == 10.0
    assert bc.max_discharge_kw == const.DEFAULT_MAX_DISCHARGE_KW == 10.0
    assert bc.max_charge_per_slot_kwh == 2.5
    assert bc.max_discharge_per_slot_kwh == 2.5


def test_coordinator_honours_configured_power():
    """Configured power reaches BatteryConfig and drives the per-slot maths."""
    bc = _coord_with(
        {const.CONF_MAX_CHARGE_KW: 4.0, const.CONF_MAX_DISCHARGE_KW: 6.0}
    )._build_battery_config()
    assert bc.max_charge_kw == 4.0
    assert bc.max_discharge_kw == 6.0
    assert bc.max_charge_per_slot_kwh == 1.0
    assert bc.max_discharge_per_slot_kwh == 1.5