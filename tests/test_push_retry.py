"""Coordinator stale-stream push retry tests (mocked).

Regression for issue #24: during a 21204 reconnect storm the Emaldo
realtime stream flaps stale<->connected every ~90 s while the optimizer
samples it once per cycle, so the v0.3.17 hard skip kept the rolling
override from ever landing in a connected window and the battery idled
on its internal AI all night.  _push_schedule now pushes through the
stale stream with a bounded retry + backoff (stale_push_retry_count,
default 3).  A non-stale stream keeps the legacy single-attempt path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.battery_optimizer import const as _const
from custom_components.battery_optimizer.coordinator import (
    BatteryOptimizerCoordinator,
    _split_result_by_day,
)
from custom_components.battery_optimizer.optimizer import OptimizationResult, SlotPlan


def _slot(index: int, action: str = "charge", slot_value: int = 0x42) -> SimpleNamespace:
    return SimpleNamespace(index=index, action=action, slot_value=slot_value)


def _coord(*, stale: bool, retries: int) -> BatteryOptimizerCoordinator:
    """Coordinator stub exercising only the _push_schedule path.

    One charge slot at index 60, now_slot pinned to 55, SoC guard off, so
    exactly one override reaches the service call.
    """
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._emaldo_control_enabled = True
    coord.hass = MagicMock()
    coord.hass.services.has_service = MagicMock(return_value=True)
    coord.hass.services.async_call = AsyncMock(return_value=None)
    # No service response support (today's ha-emaldo): plain blocking call.
    coord.hass.services.supports_response = MagicMock(return_value=None)
    # Issue #36 push bookkeeping, normally set in __init__.
    coord._last_push_at = None
    coord._last_push_ok_at = None
    coord._last_push_overrides = None
    coord._push_fail_streak = 0
    coord._last_push_fail_at = None
    coord._is_balancing_active = MagicMock(return_value=False)
    coord._emaldo_stream_stale = MagicMock(return_value=stale)
    # SoC guard interval 0 (guard off), retry count from the test.
    coord._config_int = MagicMock(
        side_effect=lambda key, default: (
            retries
            if key == _const.CONF_STALE_PUSH_RETRY_COUNT
            else 0
        )
    )
    coord._read_emaldo_internal_modes = MagicMock(return_value=None)
    coord._entry = MagicMock()
    coord._entry.data = {}
    coord._entry.options = {
        _const.CONF_IDLE_STRATEGY: _const.DEFAULT_IDLE_STRATEGY
    }
    coord._emaldo_device_id = None
    coord.resolve_emaldo_device = MagicMock()
    return coord


def _current_slot_index() -> int:
    return 55


def test_split_result_by_day_remaps_tomorrow_without_copying_totals() -> None:
    result = OptimizationResult(
        slots=[
            SlotPlan(i, "idle", 0, 0.1, 0.05)
            for i in range(192)
        ],
        total_profit=12.5,
        baseline_cost=8.0,
        emaldo_cost=6.0,
        thirdparty_pv_slots=[True] * 192,
        safeguard_slots=[95, 96],
    )

    today, tomorrow = _split_result_by_day(result)

    assert [slot.index for slot in today.slots] == list(range(96))
    assert [slot.index for slot in tomorrow.slots] == list(range(96))
    assert today.total_profit == 12.5
    assert tomorrow.total_profit == 0.0
    assert today.baseline_cost == 8.0
    assert tomorrow.baseline_cost == 0.0
    assert today.safeguard_slots == [95]
    assert tomorrow.safeguard_slots == [0]


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_stale_stream_retries_then_succeeds(sleep_mock) -> None:
    """Stale stream: service call fails twice, succeeds on the third attempt
    (retry count 2 => 3 attempts total), sleeping the backoff in between."""
    coord = _coord(stale=True, retries=2)
    coord.hass.services.async_call = AsyncMock(
        side_effect=[RuntimeError("push fail"), RuntimeError("push fail"), None]
    )
    result = SimpleNamespace(slots=[_slot(60)])

    asyncio.run(coord._push_schedule(result, None))

    assert coord.hass.services.async_call.await_count == 3
    assert sleep_mock.await_count == 2
    coord.hass.services.async_call.assert_called_with(
        _const.EMALDO_DOMAIN,
        "apply_bulk_schedule",
        {
            "slots": (
                [_const.SLOT_NO_OVERRIDE] * 55  # tomorrow region untouched
                + [_const.SLOT_IDLE] * 5        # 55..59 idle (full_control)
                + [0x42]                        # slot 60 charge
                + [_const.SLOT_IDLE] * 35       # 61..95 idle (full_control)
            )
        },
        blocking=True,
    )


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_stale_stream_all_attempts_fail_still_returns_overrides(sleep_mock) -> None:
    """Every attempt fails: error logged per attempt, backoff between each,
    and the method still returns overrides_needed (schedule kept for the
    next cycle / emitter rather than None silently).
    """
    import custom_components.battery_optimizer.coordinator as _co

    coord = _coord(stale=True, retries=2)
    coord.hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("push fail")
    )
    result = SimpleNamespace(slots=[_slot(60)])

    with patch.object(_co, "_LOGGER") as logger:
        ret = asyncio.run(coord._push_schedule(result, None))

    assert coord.hass.services.async_call.await_count == 3
    assert sleep_mock.await_count == 2
    # 41 overrides = slots 55..95; full_control idles count as enforced.
    assert ret == 41  # overrides_needed survives, never a silent None
    # One error per attempt plus the all-attempts-failed summary (issue #36).
    assert logger.error.call_count == 4
    assert "Failed to push schedule to Emaldo" in str(
        logger.error.call_args_list[0].args[0]
    )
    assert "failed on all 3 attempt(s)" in str(
        logger.error.call_args_list[3].args[0]
    )
    assert coord._last_push_ok_at is None
    assert coord._push_fail_streak == 1
    assert coord._last_push_fail_at is not None


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_successful_push_records_the_outcome(sleep_mock) -> None:
    """Issue #36: a push the service accepted stamps last_push_ok_at, resets
    the failure streak and remembers the override count."""
    coord = _coord(stale=False, retries=0)
    coord._push_fail_streak = 2
    result = SimpleNamespace(slots=[_slot(60)])

    ret = asyncio.run(coord._push_schedule(result, None))

    assert ret == 41
    assert coord._last_push_ok_at is not None
    assert coord._last_push_at == coord._last_push_ok_at
    assert coord._last_push_overrides == 41
    assert coord._push_fail_streak == 0


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_rejecting_service_response_counts_as_a_failure(sleep_mock) -> None:
    """Issue #36: when ha-emaldo answers the call, {"success": false} is a
    failed attempt, retried like an exception and recorded as a failure."""
    coord = _coord(stale=True, retries=1)
    coord.hass.services.supports_response = MagicMock(
        return_value=SimpleNamespace(value="optional")
    )
    coord.hass.services.async_call = AsyncMock(
        side_effect=[
            {"success": False, "failure_reason": "relay busy"},
            {"success": True, "failure_reason": None},
        ]
    )
    result = SimpleNamespace(slots=[_slot(60)])

    asyncio.run(coord._push_schedule(result, None))

    assert coord.hass.services.async_call.await_count == 2
    kwargs = coord.hass.services.async_call.call_args.kwargs
    assert kwargs == {"blocking": True, "return_response": True}
    assert coord._last_push_ok_at is not None
    assert coord._push_fail_streak == 0


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_guard_off_discharge_byte_is_sent_at_the_low_marker(sleep_mock) -> None:
    """Issue #38: with the SoC guard off, a planned per-slot stop level
    (discharge to 55 %) goes out as discharge-to-soc_min, and the low marker
    is stated explicitly so slot and Battery Range marker agree."""
    coord = _coord(stale=False, retries=0)
    coord._build_battery_config = MagicMock(
        return_value=SimpleNamespace(soc_min=13.0)
    )
    result = SimpleNamespace(
        slots=[_slot(60, action="discharge", slot_value=256 - 55)]
    )

    asyncio.run(coord._push_schedule(result, None))

    data = coord.hass.services.async_call.call_args.args[2]
    assert data["slots"][60] == 256 - 13
    assert data["low_marker"] == 13
    assert "high_marker" not in data
    assert coord._last_sent_slots[60] == 256 - 13
    # The plan itself keeps the planned level for sensors and charts.
    assert result.slots[0].slot_value == 256 - 55


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_guard_off_user_discharge_level_is_kept(sleep_mock) -> None:
    """Issue #38: a discharge slot set by a user rule keeps the level the
    user asked for; with no optimizer discharge slot no marker is sent."""
    coord = _coord(stale=False, retries=0)
    coord._build_battery_config = MagicMock(
        return_value=SimpleNamespace(soc_min=13.0)
    )
    sources = ["optimizer"] * 96
    sources[60] = "user"
    coord._last_sources = sources
    result = SimpleNamespace(
        slots=[_slot(60, action="discharge", slot_value=256 - 40)]
    )

    asyncio.run(coord._push_schedule(result, None))

    data = coord.hass.services.async_call.call_args.args[2]
    assert data["slots"][60] == 256 - 40
    assert "low_marker" not in data
    coord._build_battery_config.assert_not_called()


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_guard_off_tomorrow_region_uses_tomorrow_sources(sleep_mock) -> None:
    """Issue #38: positions before now_slot are tomorrow's slots, so the
    user-rule check reads tomorrow's sources there, not today's."""
    coord = _coord(stale=False, retries=0)
    coord._build_battery_config = MagicMock(
        return_value=SimpleNamespace(soc_min=13.0)
    )
    today_sources = ["optimizer"] * 96
    today_sources[10] = "user"  # today's slot 10 is in the past
    coord._last_sources = today_sources
    coord._last_sources_tomorrow = ["optimizer"] * 96
    today = SimpleNamespace(slots=[_slot(60)])
    tomorrow = SimpleNamespace(
        slots=[_slot(10, action="discharge", slot_value=256 - 55)]
    )

    asyncio.run(coord._push_schedule(today, tomorrow))

    data = coord.hass.services.async_call.call_args.args[2]
    assert data["slots"][10] == 256 - 13
    assert data["low_marker"] == 13


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_non_stale_stream_single_attempt_no_sleep(sleep_mock) -> None:
    """Healthy stream: one attempt, no backoff sleep, no retry log — the
    legacy pre-storm path."""
    coord = _coord(stale=False, retries=2)  # retries ignored when not stale
    coord.hass.services.async_call = AsyncMock(return_value=None)
    result = SimpleNamespace(slots=[_slot(60)])

    asyncio.run(coord._push_schedule(result, None))

    assert coord.hass.services.async_call.await_count == 1
    sleep_mock.assert_not_awaited()
    coord._emaldo_stream_stale.assert_called_once()


@pytest.mark.usefixtures("socket_enabled")
@patch(
    "custom_components.battery_optimizer.coordinator._current_slot_index",
    _current_slot_index,
)
@patch("asyncio.sleep", new_callable=AsyncMock)
def test_stale_stream_with_zero_retries_is_single_attempt(sleep_mock) -> None:
    """stale_push_retry_count = 0 => one-shot push-through (issue #24 comment:
    same as pre-storm behavior), no backoff."""
    coord = _coord(stale=True, retries=0)
    coord.hass.services.async_call = AsyncMock(return_value=None)
    result = SimpleNamespace(slots=[_slot(60)])

    asyncio.run(coord._push_schedule(result, None))

    assert coord.hass.services.async_call.await_count == 1
    sleep_mock.assert_not_awaited()
