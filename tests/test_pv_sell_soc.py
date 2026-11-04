"""Regression tests for PV-sell SoC forecast (#14).

`schedule.soc` (and the dashboard forecast) must NOT rise during PV-sell
windows: sold solar goes to the grid, not the battery. The bug was that
``_simulate_soc_trajectory`` (used by ``_apply_user_rules`` to re-derive
``soc_after``) ignored PV-sell flags and credited sold solar to the battery.
"""
import pytest

from custom_components.battery_optimizer.optimizer import (
    BatteryConfig,
    _simulate_soc_trajectory,
)


def _cfg() -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=5.0,
        max_charge_kw=2.5,
        max_discharge_kw=2.5,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        soc_min=10.0,
        soc_max=100.0,
        base_load_kw=0.3,
    )


def _surplus_solar(n: int, start: int, end: int):
    """Idle plan with rising solar surplus in [start, end)."""
    plan = {s: "idle" for s in range(n)}
    solar = [0.0] * n
    for s in range(start, end):
        solar[s] = 0.3 + 0.2 * (s - start)  # 0.3 .. ~3.5 kW
    net_loads = [_cfg().base_load_kw - solar[i] for i in range(n)]
    return plan, solar, net_loads


def test_sold_idle_window_does_not_raise_soc():
    """#14: idle slots sold during surplus must not raise SoC."""
    n = 96
    cfg = _cfg()
    plan, solar, net_loads = _surplus_solar(n, 28, 44)
    pv = [True] * n
    for s in range(28, 36):  # sell 07:00-09:00
        pv[s] = False
    init = cfg.capacity_kwh * cfg.soc_min / 100.0
    soc = _simulate_soc_trajectory(
        plan, net_loads, solar, cfg,
        start_slot=0, initial_soc_kwh=init, pv_slots=pv,
    )
    # SoC must not increase across the sold window.
    assert soc[35] <= soc[28] + 1e-9
    # And specifically each sold slot must not be higher than the prior.
    for s in range(28, 35):
        assert soc[s + 1] <= soc[s] + 1e-9


def test_pv_on_idle_window_charges_battery():
    """Sanity: PV-enabled idle slots during surplus still charge."""
    n = 96
    cfg = _cfg()
    plan, solar, net_loads = _surplus_solar(n, 28, 44)
    pv = [True] * n  # nothing sold
    init = cfg.capacity_kwh * cfg.soc_min / 100.0
    soc = _simulate_soc_trajectory(
        plan, net_loads, solar, cfg,
        start_slot=0, initial_soc_kwh=init, pv_slots=pv,
    )
    assert soc[35] > soc[28]


def test_sold_discharge_window_does_not_raise_soc():
    """#14: a discharge slot that is sold must not gain SoC from solar."""
    n = 96
    cfg = _cfg()
    plan = {s: "idle" for s in range(n)}
    for s in range(28, 44):
        plan[s] = "discharge"
    solar = [0.0] * n
    for s in range(28, 44):
        solar[s] = 0.3 + 0.2 * (s - 28)
    net_loads = [cfg.base_load_kw - solar[i] for i in range(n)]
    pv = [True] * n
    for s in range(28, 36):
        pv[s] = False
    init = cfg.capacity_kwh * cfg.soc_min / 100.0
    soc = _simulate_soc_trajectory(
        plan, net_loads, solar, cfg,
        start_slot=0, initial_soc_kwh=init, pv_slots=pv,
        discharge_targets={s: int(cfg.soc_min) for s in range(28, 44)},
    )
    for s in range(28, 35):
        assert soc[s + 1] <= soc[s] + 1e-9
