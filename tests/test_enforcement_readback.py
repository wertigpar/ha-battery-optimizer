"""Coordinator read-back enforcement check (issue #36), offline.

The stub's dt_util.now() is fixed at 2026-01-01 00:00 (slot 0), so the
"current slot" is index 0 of the rolling array.
"""
import asyncio
import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import tests

coord_mod = tests.load_coordinator()[0]
CoordCls = coord_mod.BatteryOptimizerCoordinator

NOW = datetime(2026, 1, 1, 0, 0)


def _chart_state(source):
    return types.SimpleNamespace(
        attributes={
            "schedule": [
                {"t": "2026-01-01T00:00:00", "price": 3.0, "source": source},
                {"t": "2026-01-01T00:15:00", "price": 3.0, "source": "override"},
            ]
        }
    )


def _mk(*, source="internal", sent=0xF8, pushed_ago_s=600):
    inst = object.__new__(CoordCls)
    inst.hass = Mock()
    inst.hass.states.get = Mock(return_value=_chart_state(source))
    inst._resolve_emaldo_entity = Mock(return_value="sensor.power_store_schedule_chart")
    inst._emaldo_control_enabled = True
    inst._last_result = object()
    inst._last_result_tomorrow = None
    inst._last_sent_slots = [sent] + [0x80] * 95
    inst._last_push_at = NOW - timedelta(seconds=pushed_ago_s)
    inst._last_push_ok_at = inst._last_push_at
    inst._push_fail_streak = 0
    inst._last_reassert_reason = None
    inst._enforcement_confirmed_at = None
    inst._enforcement_lost_at = None
    inst._enforcement_repush_count = 0
    inst._last_enforcement_repush_at = None
    inst._push_schedule = AsyncMock(return_value=41)
    return inst


def test_internal_read_back_repushes_and_records_it():
    inst = _mk(source="internal")
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_awaited_once()
    assert inst._enforcement_lost_at == NOW
    assert inst._enforcement_repush_count == 1
    assert inst._last_enforcement_repush_at == NOW
    assert inst._last_reassert_reason == "enforcement_lost"
    assert inst.enforcement_state["repush_count"] == 1
    assert inst.enforcement_state["lost_at"] == NOW.isoformat()


def test_override_read_back_confirms_without_a_push():
    inst = _mk(source="override")
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()
    assert inst._enforcement_confirmed_at == NOW
    assert inst._enforcement_lost_at is None


def test_no_override_in_the_current_slot_is_ignored():
    inst = _mk(source="internal", sent=0x80)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()


def test_missing_source_cannot_trigger_a_push():
    inst = _mk(source=None)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()
    assert inst._enforcement_lost_at is None


def test_fresh_push_is_not_judged_yet():
    inst = _mk(source="internal", pushed_ago_s=30)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()


def test_push_older_than_its_window_is_not_judged():
    inst = _mk(source="internal", pushed_ago_s=25 * 3600)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()


def test_repush_is_rate_limited():
    inst = _mk(source="internal")
    inst._last_enforcement_repush_at = NOW - timedelta(minutes=3)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()
    assert inst._enforcement_lost_at == NOW  # still recorded as lost


def test_chart_entity_missing_is_silent():
    inst = _mk(source="internal")
    inst._resolve_emaldo_entity = Mock(return_value=None)
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()


def test_control_disabled_does_nothing():
    inst = _mk(source="internal")
    inst._emaldo_control_enabled = False
    asyncio.run(inst._check_enforcement())
    inst._push_schedule.assert_not_awaited()
