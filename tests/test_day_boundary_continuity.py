"""Forecast continuity across the local-midnight day boundary.

Regression cover for the estimated-SoC seam: the speculative pre-charge used
to re-simulate only today's 96 slots *after* ``_split_result_by_day`` had
already frozen tomorrow's copy, so the two halves of the published chart
disagreed by the pre-charge's contribution (~27 SoC points) and the chart
showed a vertical cliff at 00:00 with no physical cause.

Drives ``_apply_speculative_precharge`` against a ``SimpleNamespace`` fake self,
the ``load_coordinator`` harness pattern from ``test_grid_charge_solar_aware_coord.py``.

The fixture is shaped after the *production* firing path: the day+2 hedge
(``horizon_days=2`` with tomorrow's prices known), which is the only
configuration where a pre-charge fires while a second planned day exists.
Starting SoC 60 % sits in the narrow band where the selector fires (SoC below
safety) but the base two-day plan still ends the day *below* the 66 % safety
target — at 20 % the optimizer tops up on its own and the pre-charge is a
no-op, which is what made an earlier revision of this test pass vacuously.
"""

from __future__ import annotations

from types import SimpleNamespace

from . import load_coordinator, load_optimizer

coord, const = load_coordinator()
opt, _const = load_optimizer()

SD = const.SLOTS_PER_DAY
APPLY = coord.BatteryOptimizerCoordinator._apply_speculative_precharge

CHEAP = 0.05
EXPENSIVE = 0.20

# Day+2 hedge: tomorrow's prices are known and its projected end SoC is well
# under the safety target, so the pre-charge buys tonight's cheap window as a
# hedge.  ``_prices((40, 56))`` closes before the publish cutoff, which is what
# makes it fire.  ``safety_soc_frac=0.70`` over soc_min=10 / soc_max=90 gives a
# 66 % target, so ``tomorrow_end_soc_pct=40.0`` is comfortably below it.
PRECHARGE_OPTS = dict(
    tomorrow_available=True,
    tomorrow_end_soc_pct=40.0,
    horizon_days=2,
    safety_soc_frac=0.70,
    price_ceiling=0.07,
    max_kwh_frac=0.40,
    low_solar_frac=0.25,
    require_low_solar=True,
)


def _cfg(soc_max: float = 90.0):
    return opt.BatteryConfig(
        capacity_kwh=10.0,
        max_charge_kw=2.5,
        max_discharge_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min=10.0,
        soc_max=soc_max,
        base_load_kw=0.3,
    )


def _prices(cheap_range):
    p = [EXPENSIVE] * 96
    if cheap_range:
        for s in range(*cheap_range):
            p[s] = CHEAP
    return p


def _fake_self(overrides=None):
    """Fake coordinator ``self`` exposing exactly what the pre-charge reads."""
    cfgmap = dict(PRECHARGE_OPTS)
    cfgmap.update(overrides or {})
    fake = SimpleNamespace(
        config={
            const.CONF_PRECHARGE_HORIZON_DAYS: cfgmap["horizon_days"],
            const.CONF_PRECHARGE_SAFETY_SOC: cfgmap["safety_soc_frac"],
            const.CONF_PRECHARGE_PRICE_CEILING: cfgmap["price_ceiling"],
            const.CONF_PRECHARGE_MAX_KWH_FRAC: cfgmap["max_kwh_frac"],
            const.CONF_PRECHARGE_LOW_SOLAR_FRAC: cfgmap["low_solar_frac"],
            const.CONF_PRECHARGE_REQUIRE_LOW_SOLAR: cfgmap["require_low_solar"],
        },
        _solar_regime=None,
        _locale=SimpleNamespace(
            precharge_price_ceiling=const.DEFAULT_PRECHARGE_PRICE_CEILING
        ),
        _last_sources=None,
        _precharge_publish_cutoff_slot=lambda: const.PUBLISH_CUTOFF_SLOT,
    )
    # The rewrite and re-simulation live in a helper shared with the
    # cheap-price charge (issue #31); bind it like the real coordinator has it.
    fake._force_charge_slots = (
        lambda *a, **k: coord.BatteryOptimizerCoordinator._force_charge_slots(
            fake, *a, **k
        )
    )
    return fake


def _plan_two_days():
    """Run one continuous 192-slot plan and split it, as the run does."""
    buy = _prices((40, 56))
    sell = [0.0] * (2 * SD)
    solar = [0.0] * (2 * SD)
    result = opt.optimize(
        buy + buy,
        sell,
        solar,
        _cfg(),
        initial_soc_pct=60.0,
        enable_night_drain=True,
        total_slots=2 * SD,
    )
    today, tomorrow = coord._split_result_by_day(result)
    return today, tomorrow, buy, solar


def _run_precharge(today, buy, solar, fake=None, cfg=None, **kw):
    """Apply the pre-charge to today's result; ``kw`` carries the tail."""
    APPLY(
        fake or _fake_self(),
        today,
        buy,
        solar[:SD],
        cfg or _cfg(),
        60.0,
        0,
        tomorrow_available=PRECHARGE_OPTS["tomorrow_available"],
        tomorrow_end_soc_pct=PRECHARGE_OPTS["tomorrow_end_soc_pct"],
        **kw,
    )


def test_no_soc_step_across_day_boundary_when_precharge_fires():
    """The published two-day forecast must be continuous at local midnight.

    Before the fix this fails by ~27 SoC points: the pre-charge lifts today's
    end SoC and tomorrow's copy is left at the pre-pre-charge projection.
    """
    today, tomorrow, buy, solar = _plan_two_days()

    # Guard against a vacuous pass.  Slot actions cannot carry this check — a
    # 192-slot plan always charges the cheap window on its own, so "did the
    # pre-charge fire" is only observable in today's end-of-day SoC.
    end_before = today.slots[-1].soc_after

    _run_precharge(
        today,
        buy,
        solar,
        tail_result=tomorrow,
        tail_solar=solar[SD:],
    )

    assert today.slots[-1].soc_after > end_before + 1.0, (
        "pre-charge must lift today's end SoC or this test proves nothing"
    )

    step = today.slots[-1].soc_after - tomorrow.slots[0].soc_after
    assert abs(step) < 1.0, (
        f"SoC steps {step:+.1f} points across midnight with no physical cause"
    )


def _day2_projection_end(snap, cfg, start_pct):
    """Replay day 2 alone from ``start_pct``, honouring its planned bytes.

    Independent of the production write-back: it re-derives day 2's terminal
    SoC from day 2's own actions and target bytes, started from wherever day 1
    actually finished.  ``_simulate_soc_trajectory`` falls back to
    ``charge_targets.get(s, int(cfg.soc_max))`` and
    ``discharge_targets.get(s, int(cfg.soc_min))``, so a write-back that drops
    day-2 target bytes lands on a different number than this.
    """
    actions: dict[int, str] = {}
    charge_targets: dict[int, int] = {}
    discharge_targets: dict[int, int] = {}
    for idx, action, byte in snap:
        actions[idx] = action
        if action == "charge" and 0 < byte <= 0x80:
            charge_targets[idx] = byte
        elif action == "discharge" and byte > 0x80:
            discharge_targets[idx] = 256 - byte
    traj = opt._simulate_soc_trajectory(
        actions,
        # Zero solar in this fixture, so net load is the flat base load and
        # the PV mask the simulator reads is never reached.
        [cfg.base_load_kw for _ in range(SD)],
        [0.0] * SD,
        cfg,
        start_slot=0,
        initial_soc_kwh=cfg.capacity_kwh * start_pct / 100.0,
        charge_targets=charge_targets,
        discharge_targets=discharge_targets,
    )
    return traj[-1] / cfg.capacity_kwh * 100.0


def test_day2_write_back_carries_planned_target_bytes():
    """Day 2 is re-simulated with its own target bytes, not the fallbacks.

    The simulator defaults a charge slot to "charge to soc_max" and a
    discharge slot to "stop at soc_min".  The optimizer's own default markers
    are exactly soc_max / soc_min, so those bytes agree by construction and
    prove nothing.  This fixture uses ``soc_max=100`` (the live instance runs
    12/100), which makes day 2 plan a discharge stopping well above ``soc_min``
    and a charge stopping well below ``soc_max`` — bytes where carrying or
    dropping them changes the answer.
    """
    cfg = _cfg(soc_max=100.0)
    buy = _prices((40, 56))
    solar = [0.0] * (2 * SD)
    result = opt.optimize(
        buy + buy,
        [0.0] * (2 * SD),
        solar,
        cfg,
        initial_soc_pct=60.0,
        enable_night_drain=True,
        total_slots=2 * SD,
    )
    today, tomorrow = coord._split_result_by_day(result)

    # Snapshot day 2's planned actions + target bytes before the pre-charge
    # rewrites its soc_after values.
    snap = [(sp.index, sp.action, sp.slot_value) for sp in tomorrow.slots]

    # Non-vacuity: the fixture must contain at least one day-2 target byte
    # that differs from the simulator's fallback, or the assertion below
    # cannot discriminate anything.
    assert any(
        (action == "charge" and 0 < byte <= 0x80 and byte != int(cfg.soc_max))
        or (action == "discharge" and byte > 0x80 and 256 - byte != int(cfg.soc_min))
        for _idx, action, byte in snap
    ), "fixture must contain a day-2 target byte that actually binds"

    _run_precharge(
        today,
        buy,
        solar,
        cfg=cfg,
        tail_result=tomorrow,
        tail_solar=solar[SD:],
    )

    expected_end = _day2_projection_end(snap, cfg, today.slots[-1].soc_after)
    actual_end = tomorrow.slots[-1].soc_after
    assert abs(actual_end - expected_end) < 1.0, (
        f"day 2 ends at {actual_end:.1f}% but its own projection with planned "
        f"target bytes ends at {expected_end:.1f}% — the write-back dropped "
        f"day-2 charge/discharge targets and used the soc_max/soc_min defaults"
    )


def test_run_passes_the_second_day_to_the_precharge():
    """The run must hand the pre-charge tomorrow's split result.

    Asserted on the AST: the call site and its arguments differ only in keyword
    values, which is exactly what a textual match would get wrong — the same
    reason test_grid_charge_solar_aware_coord.py uses AST for FIX 1.
    """
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "custom_components" / "battery_optimizer" / "coordinator.py"
    )
    tree = ast.parse(src.read_text(encoding="utf-8"))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_apply_speculative_precharge"
    ]
    assert calls, "no _apply_speculative_precharge call site found"

    keywords = {kw.arg for kw in calls[0].keywords}
    assert "tail_result" in keywords, "call site does not pass tail_result"
    assert "tail_solar" in keywords, "call site does not pass tail_solar"


def test_today_only_run_is_unchanged():
    """No tail_result → identical trajectory to the pre-change behaviour."""
    today_a, _, buy, solar = _plan_two_days()
    today_b, _, _, _ = _plan_two_days()

    _run_precharge(today_a, buy, solar)
    _run_precharge(today_b, buy, solar)

    assert [sp.soc_after for sp in today_a.slots] == [
        sp.soc_after for sp in today_b.slots
    ]
    assert any(sp.action == "charge" for sp in today_a.slots[40:56]), (
        "pre-charge must still fire today-only, otherwise this proves nothing"
    )

    # The equality above can only catch nondeterminism, so pin the invariant it
    # stands for directly: carrying a tail must leave TODAY's trajectory
    # bit-for-bit identical.  This is the guard against day-2 data leaking into
    # day 1 — e.g. a tail loop keyed by the bare day-2 index, which would
    # overwrite plan_actions[0..95] with tomorrow's actions.
    today_c, tail_c, _, _ = _plan_two_days()
    _run_precharge(
        today_c,
        buy,
        solar,
        tail_result=tail_c,
        tail_solar=solar[SD:],
    )

    assert [sp.soc_after for sp in today_c.slots] == [
        sp.soc_after for sp in today_a.slots
    ], (
        "supplying tail_result changed today's trajectory — today-only and "
        "tail runs must simulate day 1 identically"
    )


def test_slot_counts_cover_today_only():
    """charge/discharge/idle counts must not absorb tomorrow's slots."""
    today, tomorrow, buy, solar = _plan_two_days()
    before = (today.charge_slots, today.discharge_slots, today.idle_slots)

    _run_precharge(today, buy, solar, tail_result=tomorrow,
                   tail_solar=solar[SD:])

    assert sum((today.charge_slots, today.discharge_slots, today.idle_slots)) == SD, (
        f"counts {today.charge_slots}/{today.discharge_slots}/{today.idle_slots} "
        f"no longer partition today's {SD} slots (was {before})"
    )


def test_day2_charge_raises_day2_forecast():
    """Day 2's own grid-charge slots must move the day-2 forecast.

    The baseline is a control run of the SAME fixture in which day 2's
    20:30 window is left as the plan made it, so the only variable between
    the two runs is whether those slots are forced to charge.  A baseline
    read from day-2 slots 40:50 *after* the run cannot discriminate: forcing
    20:30 to charge fills the battery, so 40:50 saturates at soc_max too and
    both maxima read 90.0 -- the comparison becomes 90.0 > 95.0.  The control
    run also keeps the assertion from passing on the seam fix alone, which
    moves day 2 whether or not day 2's own actions are simulated.
    """
    today_ctl, tail_ctl, buy, solar = _plan_two_days()
    today_forced, tail_forced, _, _ = _plan_two_days()
    for sp in tail_forced.slots[20:30]:
        sp.action = "charge"

    _run_precharge(
        today_ctl, buy, solar, tail_result=tail_ctl, tail_solar=solar[SD:],
    )
    _run_precharge(
        today_forced, buy, solar, tail_result=tail_forced, tail_solar=solar[SD:],
    )

    charged = [sp.soc_after for sp in tail_forced.slots[20:30]]
    baseline = [sp.soc_after for sp in tail_ctl.slots[20:30]]
    assert max(charged) > max(baseline) + 5.0, (
        f"day-2 charge slots peaked at {max(charged):.1f}% against "
        f"{max(baseline):.1f}% with the same slots left alone -- the tail is "
        f"not being simulated"
    )


def test_sparse_tail_does_not_truncate_the_horizon(monkeypatch):
    """n_sim must be sized by max index, not by len(plan_actions).

    A sparse tail makes len(plan_actions) < max_index + 1, which would
    truncate the simulation and leave stale soc_after on the slots past
    the truncation point -- the silent failure this fix exists to remove.
    """
    today, tomorrow, buy, solar = _plan_two_days()

    # Hand-built sparse tail: a split that kept only day-2 slots 40..47, so
    # len(tail.slots) is 8 while the highest horizon key is SD + 47 == 143.
    tail_slots = tomorrow.slots[40:48]
    highest_index = tail_slots[-1].index
    assert len(tail_slots) < highest_index + 1, (
        "fixture must be sparse: fewer slots than the highest index"
    )
    pre_apply = {sp.index: sp.soc_after for sp in tail_slots}
    for sp in tail_slots:
        sp.soc_after = -999.0

    seen = {}
    real_simulate = coord._simulate_soc_trajectory

    def _spy(*args, **kwargs):
        seen["total_slots"] = kwargs["total_slots"]
        return real_simulate(*args, **kwargs)

    monkeypatch.setattr(coord, "_simulate_soc_trajectory", _spy)

    _run_precharge(
        today,
        buy,
        solar,
        tail_result=SimpleNamespace(
            slots=tail_slots, thirdparty_pv_slots=[]
        ),
        tail_solar=solar[SD:],
    )

    assert seen["total_slots"] == SD + highest_index + 1, (
        f"simulated {seen['total_slots']} slots but the horizon needs "
        f"{SD + highest_index + 1} to reach tail index {highest_index} — "
        f"n_sim is sized by key count, not max index"
    )
    still_stale = [
        sp.index for sp in tail_slots if sp.soc_after == -999.0
    ]
    assert not still_stale, (
        f"tail slots {still_stale} kept their pre-apply soc_after "
        f"(sentinel survived the write-back); the simulated horizon stops "
        f"short of the highest tail index, so those values go out stale"
    )
    assert all(
        abs(sp.soc_after - pre_apply[sp.index]) > 0.05 for sp in tail_slots
    ), "tail soc_after must come from the new simulation, not be preserved"
