"""Regression tests for issue #36."""
import tests

coord_mod = tests.load_coordinator()[0]
CoordCls = coord_mod.BatteryOptimizerCoordinator


def _mk():
    import types
    from datetime import datetime, timedelta
    from unittest.mock import Mock

    inst = object.__new__(CoordCls)
    inst.hass = Mock()
    inst.config_entry = types.SimpleNamespace(options={}, data={})
    inst._last_push_at = None
    inst._last_push_ok_at = None
    inst._last_push_overrides = None
    inst._last_sent_slots = None
    inst._push_fail_streak = 0
    inst._last_push_fail_at = None
    inst._enforcement_reassert_min_s = 1800
    inst._accuracy_reassert_shortfall_kwh = 0.35
    inst._accuracy_reassert_ratio = 0.25
    inst._accuracy_reassert_cooldown_s = 900
    inst._last_plan_accuracy_for_reassert = None
    inst._last_reassert_reason = None
    inst._emaldo_control_enabled = True
    return inst, datetime, timedelta


def test_reassert_on_no_successful_push():
    inst, dt, _ = _mk()
    assert inst._should_reassert_override()[0] is True
    assert inst._should_reassert_override()[1] == "no_successful_push"


def test_no_reassert_if_recent_ok():
    inst, dt, td = _mk()
    inst._last_push_ok_at = inst._last_push_at = dt.now() - td(minutes=10)
    assert inst._should_reassert_override()[0] is False


def test_reassert_on_stale_ok():
    inst, dt, td = _mk()
    # module dt_util.now returns 2026-01-01 in tests
    inst._last_push_ok_at = dt(2026, 1, 1) - td(minutes=40)
    inst._last_push_at = inst._last_push_ok_at
    ok, why = inst._should_reassert_override()
    assert ok and why == "stale_push"


def test_reassert_on_fail_streak():
    inst, dt, td = _mk()
    inst._last_push_fail_at = dt.now() - td(minutes=2)
    inst._push_fail_streak = 1
    ok, why = inst._should_reassert_override()
    assert ok and why == "push_failed_recently"


def test_no_accuracy_reassert_inside_cooldown():
    inst, dt, td = _mk()
    inst._accuracy_reassert_cooldown_s = 900
    inst._last_push_ok_at = dt(2026, 1, 1) - td(minutes=5)
    inst._last_plan_accuracy_for_reassert = {
        "elapsed_slots": 4,
        "planned_discharge_kwh": 0.84,
        "actual_discharge_kwh": 0.02,
    }
    assert inst._should_reassert_override() == (False, None)


def test_reassert_on_accuracy_divergence():
    inst, dt, td = _mk()
    inst._accuracy_reassert_cooldown_s = 900
    inst._last_push_ok_at = dt(2026, 1, 1) - td(minutes=20)
    inst._last_plan_accuracy_for_reassert = {
        "elapsed_slots": 4,
        "planned_discharge_kwh": 0.84,
        "actual_discharge_kwh": 0.02,
    }
    ok, why = inst._should_reassert_override()
    assert ok and why == "accuracy_divergence"
