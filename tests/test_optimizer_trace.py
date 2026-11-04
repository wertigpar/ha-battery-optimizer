"""Decision-trace tests: main runs carry a trace, probes do not."""
from custom_components.battery_optimizer.optimizer import (
    BatteryConfig, OptimizationResult, _probe_result, optimize,
)

N = 96
BUY = [0.05] * N
for i in range(72, 88):
    BUY[i] = 0.25 + 0.004 * i  # evening ramp
SELL = [0.002] * N
SOLAR = [0.0] * N
CFG = BatteryConfig(capacity_kwh=15.0)


def test_main_run_has_trace_with_gate_keys():
    res = optimize(BUY, SELL, SOLAR, CFG, start_slot=76, initial_soc_pct=100.0)
    assert isinstance(res.trace, dict)
    for key in (
        "initial_soc_pct", "case_a_floor", "refill_fraction",
        "solar_full_recharge", "total_discharge_budget",
        "n_profitable_discharge", "mode", "night_drain_applied",
    ):
        assert key in res.trace, key


def test_probe_has_no_trace():
    probe = _probe_result(BUY, SELL, SOLAR, CFG, 76, 100.0, False, None, CFG.soc_floor_target_pct)
    assert probe.trace is None


def test_trace_matches_the_money():
    res = optimize(BUY, SELL, SOLAR, CFG, start_slot=76, initial_soc_pct=100.0)
    # budget equals usable energy above soc_min + reserve at full SoC
    assert abs(
        res.trace["total_discharge_budget"]
        - (100.0 - CFG.soc_min - CFG.soc_recovery_buffer_pct) / 100.0 * CFG.capacity_kwh
    ) < 0.01


def test_plan_unchanged_by_trace():
    a = optimize(BUY, SELL, SOLAR, CFG, start_slot=76, initial_soc_pct=100.0)
    b = optimize(BUY, SELL, SOLAR, CFG, start_slot=76, initial_soc_pct=100.0)
    assert [(s.index, s.action) for s in a.slots] == [(s.index, s.action) for s in b.slots]