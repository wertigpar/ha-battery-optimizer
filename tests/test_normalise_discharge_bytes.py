"""Issue #38: optimizer discharge bytes are sent at the low marker.

Runs without Home Assistant (test_push_retry covers the same through
_push_schedule but needs a real install to collect).
"""
import types

import tests

mod = tests.load_coordinator()[0]
C = mod.BatteryOptimizerCoordinator


def _inst(sources=None, tomorrow=None, soc_min=13.0, marker=None):
    o = object.__new__(C)
    o._build_battery_config = lambda: types.SimpleNamespace(soc_min=soc_min)
    o._entry = types.SimpleNamespace(
        data={} if marker is None else {mod.CONF_BATTERY_RANGE_LOW_MARKER: marker},
        options={},
    )
    o._last_sources = sources
    o._last_sources_tomorrow = tomorrow
    return o


def test_optimizer_discharge_goes_out_at_the_low_marker():
    sv = [128] * 96
    sv[60] = 256 - 55  # planned stop level 55 %
    sv[10] = 5  # a charge byte
    assert _inst()._normalise_discharge_bytes(sv, 0) == 13
    assert sv[60] == 256 - 13
    assert sv[10] == 5 and sv[0] == 128


def test_user_rule_level_is_kept():
    sv = [128] * 96
    sv[60], sv[61] = 256 - 40, 256 - 70
    src = ["optimizer"] * 96
    src[60] = "user"
    assert _inst(sources=src)._normalise_discharge_bytes(sv, 0) == 13
    assert sv[60] == 256 - 40
    assert sv[61] == 256 - 13


def test_no_marker_when_only_user_slots_discharge():
    sv = [128] * 96
    sv[60] = 256 - 40
    src = ["optimizer"] * 96
    src[60] = "user"
    assert _inst(sources=src)._normalise_discharge_bytes(sv, 0) is None
    assert sv[60] == 256 - 40


def test_rolling_window_reads_tomorrows_sources_before_now_slot():
    sv = [128] * 96
    sv[5] = sv[80] = 256 - 30
    tom = ["optimizer"] * 96
    tom[5] = "user"
    o = _inst(sources=["optimizer"] * 96, tomorrow=tom)
    o._normalise_discharge_bytes(sv, 40)
    assert sv[5] == 256 - 30  # tomorrow's user slot
    assert sv[80] == 256 - 13  # today's optimizer slot


def test_soc_min_zero_never_produces_the_idle_byte():
    sv = [128] * 96
    sv[60] = 256 - 55
    assert _inst(soc_min=0.0)._normalise_discharge_bytes(sv, 0) == 1
    assert sv[60] == 255  # 0 would be the idle byte


def test_configured_marker_overrides_the_planned_floor():
    sv = [128] * 96
    sv[60] = 256 - 55
    assert _inst(soc_min=31.0, marker=7.0)._normalise_discharge_bytes(sv, 0) == 7
    assert sv[60] == 256 - 7


def test_configured_marker_is_bounded_like_the_floor():
    sv = [128] * 96
    sv[60] = 256 - 55
    assert _inst(marker=0.0)._normalise_discharge_bytes(sv, 0) == 1
    assert sv[60] == 255
