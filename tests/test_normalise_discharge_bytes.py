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


def _cfg(soc_min=13.0):
    return types.SimpleNamespace(soc_min=soc_min)


def test_resolved_low_marker_follows_the_floor_when_unset():
    assert _inst(soc_min=13.0)._resolved_low_marker(_cfg(13.0), None) == 13


def test_resolved_low_marker_uses_the_configured_marker():
    assert _inst(soc_min=31.0, marker=7.0)._resolved_low_marker(_cfg(31.0), None) == 7


def test_resolved_low_marker_clamps_to_the_high_marker():
    assert _inst(marker=40.0)._resolved_low_marker(_cfg(13.0), 25) == 25


def test_resolved_low_marker_never_returns_the_idle_byte():
    assert _inst(marker=0.0)._resolved_low_marker(_cfg(13.0), None) == 1


def test_low_marker_clamp_warning_logs_once_per_conflict(caplog):
    import logging

    o = _inst(marker=40.0)
    with caplog.at_level(logging.WARNING, logger=mod._LOGGER.name):
        assert o._resolved_low_marker(_cfg(13.0), 25) == 25
        assert o._resolved_low_marker(_cfg(13.0), 25) == 25
    assert caplog.text.count("Battery Range low marker") == 1
    with caplog.at_level(logging.WARNING, logger=mod._LOGGER.name):
        o._resolved_low_marker(_cfg(13.0), 30)
    assert caplog.text.count("Battery Range low marker") == 2
    with caplog.at_level(logging.WARNING, logger=mod._LOGGER.name):
        o._resolved_low_marker(_cfg(13.0), None)
    assert caplog.text.count("Battery Range low marker") == 2


def test_fgr_frame_is_unchanged_for_normal_installs():
    # FGR-shaped config: zero reserves, marker unset.  The pushed frame must
    # stay the pre-#42 one: marker == the planned floor (bounded to 1..99)
    # and every discharge byte == 256 - marker, never the idle byte 0.
    for soc_min in (0, 13, 99):
        sv = [128] * 96
        discharge_slots = (5, 60, 90)
        for pos in discharge_slots:
            sv[pos] = 256 - 55
        marker = _inst(soc_min=float(soc_min))._normalise_discharge_bytes(sv, 0)
        expected = max(1, min(99, int(soc_min)))
        assert marker == expected
        for pos in discharge_slots:
            assert sv[pos] == 256 - expected
        assert 0 not in sv


def test_guard_marker_never_below_the_idle_byte():
    for soc_min, expected in ((0.0, 1), (-5.0, 1), (13.0, 13)):
        o = _inst(soc_min=soc_min)
        o._last_result = None
        marker = o._compute_soc_guard_marker()
        assert marker == expected
        assert marker >= mod.MARKER_MIN
