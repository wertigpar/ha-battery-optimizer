"""Regression test for issue #33: PV sell transitions raised UnboundLocalError.

``_apply_pv_strategy`` schedules a callback per slot-boundary state change.
The callback assigned ``switch_on = False`` for the full-battery cut (#28),
which made ``switch_on`` local to the callback, so every call failed before
reading it.  The coordinator cannot be instantiated (it needs HA), so the
unbound method is driven against a stub, with ``async_call_later`` capturing
the scheduled callbacks.
"""
import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import tests

coord_mod = tests.load_coordinator()[0]
CoordCls = coord_mod.BatteryOptimizerCoordinator

N = 96
CURRENT_SLOT = 40  # 10:00


def _run(cut_active: bool):
    """Schedule transitions for PV [on, off, on] and fire each callback once.

    Returns the list of ``switch_on`` values the callbacks asked for.
    """
    captured = []
    set_calls = []

    pv_slots = [True] * N
    pv_slots[CURRENT_SLOT + 1] = False  # boundary 10:15 -> off
    # slot CURRENT_SLOT + 2 stays True -> boundary 10:30 -> on

    hass = types.SimpleNamespace(
        async_create_task=lambda coro: set_calls.append(coro)
    )

    async def _set_pv_switch(on):
        return on

    async def _ensure(_desired):
        return None

    stub = types.SimpleNamespace(
        hass=hass,
        config={},
        _unsub_pv_transitions=[],
        _emaldo_control_enabled=True,
        _pv_strategy_enabled=True,
        _pv_cut_latch=False,
        _last_result=types.SimpleNamespace(thirdparty_pv_slots=pv_slots),
        _get_solcast_forecast=lambda _day: [4.0] * N,
        _desired_pv_state_now=lambda _r: True,
        _ensure_pv_switch_matches_plan=_ensure,
        _pv_cut_active=lambda _r: cut_active,
        _set_pv_switch=_set_pv_switch,
    )

    saved = (
        coord_mod._current_slot_index,
        coord_mod.async_call_later,
        coord_mod.dt_util,
    )
    coord_mod._current_slot_index = lambda: CURRENT_SLOT
    coord_mod.async_call_later = (
        lambda _hass, _delay, cb: captured.append(cb) or (lambda: None)
    )
    coord_mod.dt_util = types.SimpleNamespace(
        now=lambda: datetime(2026, 10, 30, 10, 0, 0)
    )
    try:
        result = stub._last_result
        asyncio.run(CoordCls._apply_pv_strategy(stub, result))
    finally:
        (
            coord_mod._current_slot_index,
            coord_mod.async_call_later,
            coord_mod.dt_util,
        ) = saved

    assert len(captured) == 2, "expected one callback per state change"
    for cb in captured:
        cb(None)  # must not raise UnboundLocalError
    return [asyncio.run(c) for c in set_calls]


def test_transition_callbacks_run_without_cut():
    assert _run(cut_active=False) == [False, True]


def test_transition_callbacks_hold_pv_off_when_cut_active():
    # The cut overrides the sell-strategy "on" transition, and must not leak
    # into the next call of the same callback (closure stays unchanged).
    assert _run(cut_active=True) == [False, False]
