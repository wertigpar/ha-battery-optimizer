"""TDD test for the full-battery third-party PV cut (issue #28).

The coordinator class cannot be instantiated directly (it needs HA), so the
cut logic is exercised by binding BatteryOptimizerCoordinator._pv_cut_active
and _desired_pv_state_now onto a stub that carries the instance state the
methods read, and by monkeypatching the module-level _current_slot_index.
"""
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import tests
from tests import load_optimizer

opt, _const = load_optimizer()
coord_mod = tests.load_coordinator()[0]
CoordCls = coord_mod.BatteryOptimizerCoordinator

N = opt.SLOTS_PER_DAY  # 96
CURRENT_SLOT = 40  # fixed daytime slot for determinism

# Real const string keys (the coordinator imports them from .const).
CONF_SOC_MAX = "soc_max"
CONF_SOLAR_SELL_MIN = "solar_sell_min_forecast_kwh"
CONF_ENABLE_PV_CUT = "enable_pv_cut_when_full"
CONF_PV_CUT_RESUME = "pv_cut_resume_soc"
DEFAULT_SOC_MAX = 100
DEFAULT_SOLAR_SELL_MIN = 10.0
DEFAULT_ENABLE_PV_CUT = False
DEFAULT_PV_CUT_RESUME = 97.0


class FakeConfig:
    """dict-like config with .get(key, default)."""
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        return self._v.get(key, default)


def _slot(action="idle", soc_after=50.0):
    return opt.SlotPlan(
        index=0, action=action, slot_value=0.0, soc_after=soc_after,
        load_kw=0.0, solar_kw=0.0, buy_price=0.0, sell_price=0.0,
    )


def _result(slots):
    """slots: list of (action, soc_after) tuples, length N."""
    plan = [_slot(a, s) for (a, s) in slots]
    r = opt.OptimizationResult()
    r.slots = plan
    r.thirdparty_pv_slots = [True] * N
    return r


def make_stub(cfg_values, soc, result, solar_today=None):
    stub = types.SimpleNamespace()
    stub.config = FakeConfig(cfg_values)
    stub._pv_strategy_enabled = True
    stub._last_known_soc = soc
    stub._last_result = result
    stub._pv_cut_latch = False
    stub._solar_today = solar_today if solar_today is not None else [4.0] * N

    def _get_solcast_forecast(which):
        return list(stub._solar_today)

    stub._get_solcast_forecast = _get_solcast_forecast
    stub._pv_cut_active = types.MethodType(CoordCls._pv_cut_active, stub)
    stub._desired_pv_state_now = types.MethodType(CoordCls._desired_pv_state_now, stub)
    return stub


def base_cfg(**over):
    d = {
        CONF_SOC_MAX: DEFAULT_SOC_MAX,
        CONF_SOLAR_SELL_MIN: DEFAULT_SOLAR_SELL_MIN,
        CONF_ENABLE_PV_CUT: DEFAULT_ENABLE_PV_CUT,
        CONF_PV_CUT_RESUME: DEFAULT_PV_CUT_RESUME,
    }
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def _fixed_slot(monkeypatch):
    """Pin the current slot so the test does not depend on the wall clock."""
    monkeypatch.setattr(coord_mod, "_current_slot_index", lambda: CURRENT_SLOT)


def test_cut_disabled_by_default():
    """With enable_pv_cut_when_full off, PV follows the sell strategy."""
    result = _result([("idle", 95.0)] * N)
    stub = make_stub(base_cfg(), 95.0, result)
    assert stub._desired_pv_state_now(result) is True
    assert stub._pv_cut_latch is False


def test_cut_fires_when_battery_full_not_charging():
    """Full SoC, sunny, idle slot -> PV cut."""
    result = _result([("idle", 100.0)] * N)
    stub = make_stub(base_cfg(enable_pv_cut_when_full=True), 100.0, result)
    assert stub._desired_pv_state_now(result) is False
    assert stub._pv_cut_latch is True


def test_cut_resumes_below_threshold():
    """Once fired, the cut stays latched until SoC < pv_cut_resume_soc."""
    result = _result([("idle", 100.0)] * N)
    stub = make_stub(base_cfg(enable_pv_cut_when_full=True), 100.0, result)
    assert stub._desired_pv_state_now(result) is False  # fires at 100
    assert stub._pv_cut_latch is True
    # SoC 99 -> still above 97 resume -> stays cut
    stub._last_known_soc = 99.0
    assert stub._desired_pv_state_now(result) is False
    assert stub._pv_cut_latch is True
    # SoC 96.9 -> below resume -> PV restored
    stub._last_known_soc = 96.9
    assert stub._desired_pv_state_now(result) is True
    assert stub._pv_cut_latch is False


def test_cut_never_fires_during_charge_slot():
    """A charge slot needs third-party PV, so the cut does not fire at full."""
    plan = [("idle", 100.0)] * N
    plan = list(plan)
    plan[CURRENT_SLOT] = ("charge", 100.0)
    result = _result(plan)
    stub = make_stub(base_cfg(enable_pv_cut_when_full=True), 100.0, result)
    assert stub._desired_pv_state_now(result) is True
    assert stub._pv_cut_latch is False


def test_cut_never_fires_cloudy_day():
    """Cloudy day = no surplus = no pulses, so the cut does not fire."""
    result = _result([("idle", 100.0)] * N)
    stub = make_stub(base_cfg(enable_pv_cut_when_full=True), 100.0, result,
                     solar_today=[0.0] * N)
    assert stub._desired_pv_state_now(result) is True
    assert stub._pv_cut_latch is False


def test_cut_uses_planned_soc_when_live_unknown():
    """When live SoC is unavailable the planned SoC is used."""
    result = _result([("idle", 100.0)] * N)
    stub = make_stub(base_cfg(enable_pv_cut_when_full=True), None, result)
    assert stub._desired_pv_state_now(result) is False
    assert stub._pv_cut_latch is True


@pytest.mark.parametrize("pv_at_current", [True, False])
def test_opt_out_is_a_no_op(pv_at_current):
    """With enable_pv_cut_when_full OFF, PV follows thirdparty_pv_slots exactly.

    The cut must not change the switch state for any user who has not enabled it,
    regardless of SoC or the slot action.
    """
    slots = [("idle", 100.0)] * N
    result = _result(slots)
    result.thirdparty_pv_slots = [pv_at_current] * N
    # Full SoC and a charge slot would both trigger the cut if it were on;
    # with the feature off neither changes the answer.
    for soc in (100.0, 50.0, 96.9):
        stub = make_stub(base_cfg(), soc, result)
        assert stub._desired_pv_state_now(result) is pv_at_current
        assert stub._pv_cut_latch is False