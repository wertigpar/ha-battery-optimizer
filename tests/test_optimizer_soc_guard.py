"""Coordinator battery-SoC guard tests (mocked).

Regression for issue #16: an unreadable battery SoC used to silently
degrade the plan (soc_min fallback => zero discharge budget => all-idle
plan pushed over the profitable evening window).  run_optimizer now
refuses to run with no SoC history and falls back to the last known
value otherwise.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.util import dt as dt_util

from custom_components.battery_optimizer.coordinator import (
    BatteryOptimizerCoordinator,
)
from custom_components.battery_optimizer.optimizer import BatteryConfig


def _coord() -> BatteryOptimizerCoordinator:
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._last_known_soc = None
    coord._soc_live_read_done = False
    coord._ran_this_session = False
    coord._soc_wait_start = None
    coord._get_battery_soc = MagicMock(return_value=None)
    coord._maybe_sweep_expired_rules = AsyncMock()
    coord._restore_runtime_state = AsyncMock()
    # Run snapshot capture — always mocked so no executor/file I/O happens.
    coord._capture_run_snapshot = AsyncMock()
    # Prices decoder — must never be reached when the SoC guard bails.
    coord._parse_price_data = MagicMock(
        side_effect=AssertionError("price parsing reached on unreadable SoC")
    )
    return coord


def _export_coord() -> BatteryOptimizerCoordinator:
    """Coordinator stub that only needs the export path."""
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._last_snapshot = None
    coord._last_planned_snapshot = None
    coord.hass = MagicMock()
    coord.hass.config.path = lambda name: f"/config/{name}"
    coord.hass.async_add_executor_job = AsyncMock()
    return coord


def test_export_prefers_planned_over_skip_snapshot():
    coord = _export_coord()
    skip = {"state": "no_change_skip", "planned": False, "reason": "checkpoint"}
    planned = {"state": "planned", "planned": True, "reason": "manual_button"}
    coord._last_snapshot = skip
    coord._last_planned_snapshot = planned
    asyncio.run(coord.export_plan_analysis())
    coord.hass.async_add_executor_job.assert_awaited_once()
    args = coord.hass.async_add_executor_job.await_args.args
    func, path, md = args[0], args[1], args[2]
    assert path == "/config/battery_optimizer_analysis.md"
    assert "## Planned schedule" in md   # rendered from the planned snapshot
    assert "Skipped run" not in md


def test_export_falls_back_to_skip_when_no_planned():
    coord = _export_coord()
    skip = {"state": "soc_guard_skip", "planned": False, "reason": "checkpoint"}
    coord._last_snapshot = skip
    asyncio.run(coord.export_plan_analysis())
    args = coord.hass.async_add_executor_job.await_args.args
    md = args[2]
    assert "Skipped run" in md


def test_export_returns_none_when_no_snapshot():
    coord = _export_coord()
    assert asyncio.run(coord.export_plan_analysis()) is None
    coord.hass.async_add_executor_job.assert_not_awaited()


def test_unreadable_soc_without_history_skips_run():
    coord = _coord()
    result = asyncio.run(coord.run_optimizer(reason="test", force=True))
    assert result is None
    coord._parse_price_data.assert_not_called()


def test_unreadable_soc_uses_last_known_value():
    coord = _coord()
    coord._last_known_soc = 87.0
    coord._soc_live_read_done = True  # a live read succeeded earlier this session
    # Guard passes -> run proceeds to price parsing; stop it there cleanly.
    coord._parse_price_data = MagicMock(return_value=(None, None))
    result = asyncio.run(coord.run_optimizer(reason="test", force=True))
    coord._parse_price_data.assert_called_once()
    # last-known survives the run unchanged
    assert coord._last_known_soc == 87.0


def test_unreadable_soc_waits_when_no_live_read():
    """Fresh restart: a restored (stale) last-known value must NOT be reused;
    the guard waits for a live reading instead of pushing a plan."""
    coord = _coord()
    coord._last_known_soc = 33.4  # stale projection from restored runtime state
    coord._soc_live_read_done = False  # no live read this session yet
    coord._parse_price_data = MagicMock(
        side_effect=AssertionError("price parsing reached on unreadable SoC")
    )
    result = asyncio.run(coord.run_optimizer(reason="test", force=True))
    assert result is None
    coord._parse_price_data.assert_not_called()


def test_wait_emits_one_minute_warning():
    """After 60s of waiting for the first live read, a single warning is
    emitted; the per-trigger 'waiting' message stays suppressed (debug)."""
    coord = _coord()
    coord._last_known_soc = 33.4
    coord._soc_live_read_done = False
    coord._soc_wait_start = dt_util.now() - timedelta(seconds=90)
    coord._parse_price_data = MagicMock(
        side_effect=AssertionError("price parsing reached on unreadable SoC")
    )
    import custom_components.battery_optimizer.coordinator as _co
    with patch.object(_co, "_LOGGER") as logger:
        asyncio.run(coord.run_optimizer(reason="test", force=True))
    assert any(
        "still unreadable after 1 minute" in str(c.args[0])
        for c in logger.warning.call_args_list
    )
    # The first-wait debug message is the only debug; no per-trigger warning.
    assert not any(
        "waiting for first live reading" in str(c.args[0])
        for c in logger.warning.call_args_list
    )


def test_startup_not_defeated_by_restored_state():
    """The startup watcher must still run after a restart even though state
    restoration set _last_result (yesterday's plan).  It keys on
    _ran_this_session, not _last_result."""
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._ran_this_session = False
    coord._last_result = object()  # truthy — would defeat the old check
    coord._startup_attempts = 0
    coord._unsub_startup = None
    coord._STARTUP_MAX_ATTEMPTS = 4
    coord._get_battery_soc = MagicMock(return_value=None)
    coord._schedule_startup_run = MagicMock()
    coord.hass = MagicMock()
    coord.hass.async_create_task = MagicMock()

    coord._startup_callback(None)

    # Did NOT return early — advanced to the SoC check and retried.
    assert coord._startup_attempts == 1
    coord._schedule_startup_run.assert_called_once()


def test_startup_skips_when_already_ran_this_session():
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._ran_this_session = True  # a run already completed this session
    coord._last_result = None
    coord._startup_attempts = 0
    coord._unsub_startup = None
    coord._schedule_startup_run = MagicMock()

    coord._startup_callback(None)

    assert coord._startup_attempts == 0
    coord._schedule_startup_run.assert_not_called()


def test_successful_soc_stored_as_last_known():
    coord = _coord()
    coord._get_battery_soc = MagicMock(return_value=100.0)
    coord._parse_price_data = MagicMock(return_value=(None, None))
    asyncio.run(coord.run_optimizer(reason="test", force=True))
    assert coord._last_known_soc == 100.0


def test_restore_sets_last_known_soc_from_plan():
    """Restart guard (3:14 PM HA-restart error): _restore_runtime_state must
    restore _last_known_soc from the last plan slot's projected end-SoC, so
    the SoC guard falls back instead of skipping when Emaldo has not yet
    published its initial reading (Optimizer boots faster than Emaldo).

    Without this, a same-day restart with no fresh SoC read skips the run and
    keeps a stale prior schedule.
    """
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._runtime_state_restored = False
    coord._last_known_soc = None
    today = dt_util.now().date().isoformat()
    data = {
        "last_run_slot": 42,
        "last_run_initial_soc": 38.0,
        "last_run_scale": 1.0,
        "last_run": f"{today}T14:00:00",
        "snapshot": {},
        "plan_slots": [[42, "idle", 38.5, 0.0], [95, "idle", 52.0, 0.0]],
    }
    coord._load_runtime_state = MagicMock(return_value=data)
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=data)

    asyncio.run(coord._restore_runtime_state())

    # Last plan slot's projected end-SoC becomes the last-known value.
    assert coord._last_known_soc == 52.0
    assert coord._runtime_state_restored is True


def test_restore_no_state_leaves_last_known_soc_none():
    """With no persisted runtime state, _last_known_soc stays None and the
    SoC guard still skips (genuinely no history — acceptable)."""
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._runtime_state_restored = False
    coord._last_known_soc = None
    coord._load_runtime_state = MagicMock(return_value=None)
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)

    asyncio.run(coord._restore_runtime_state())

    assert coord._last_known_soc is None


def test_planned_run_captures_snapshot():
    """A successful run ends with a 'planned' snapshot carrying soc_input.

    _parse_price_data's real contract is tuple[list[float] | None,
    list[float] | None] of 96 x €/kWh spot values — so a flat 96-float
    vector (constant 5 ct/kWh) lets the run reach the finish line and the
    full success snapshot, proving the end-of-run capture (not just the
    no-prices skip).
    """
    coord = _coord()
    coord._get_battery_soc = MagicMock(return_value=100.0)
    coord._parse_price_data = MagicMock(return_value=([0.05] * 96, None))
    # Success-path dependencies (all stubbed — run_optimizer reaches the end).
    coord._actual_soc = []
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass.states = MagicMock()
    coord._resolve_solar_scale = MagicMock(return_value=1.0)
    coord._get_solcast_forecast = MagicMock(return_value=[0.0] * 96)
    coord._fetch_auto_base_load_kw = AsyncMock(return_value=None)
    coord._build_battery_config = MagicMock(return_value=BatteryConfig())
    coord._compute_plan_accuracy = MagicMock(return_value=None)
    coord._read_emaldo_internal_modes = MagicMock(return_value=[])
    coord._read_user_rules = MagicMock(return_value=[])
    coord._read_emaldo_sensor_float = MagicMock(return_value=None)
    coord._entry = MagicMock()
    coord._entry.data = {}
    coord._entry.options = {}
    coord._entry.entry_id = "test-entry"
    coord._emaldo_control_enabled = False
    coord._pv_strategy_enabled = False
    coord._push_schedule = AsyncMock(return_value=None)
    coord._apply_pv_strategy = AsyncMock()
    coord._compute_activated_time = MagicMock(return_value=None)
    coord._activated_time = None
    coord.async_set_updated_data = MagicMock()
    captured = AsyncMock()
    coord._capture_run_snapshot = captured

    asyncio.run(coord.run_optimizer(reason="test", force=True))

    assert captured.await_count >= 1
    snap = captured.await_args.args[0]
    assert snap["state"] == "planned"
    assert snap["soc_input"] == "read"
    assert snap["planned"] is True
    assert snap["initial_soc_pct"] == 100.0


def _checkpoint_coord():
    """Coordinator stub for the periodic checkpoint callback.

    async_create_task swallows the produced coroutine (closes it) so the
    AsyncMock's recorded call args can be inspected without a running loop.
    """
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._last_result_tomorrow = None
    coord._has_tomorrow_prices = MagicMock(return_value=False)
    coord.run_optimizer = AsyncMock()
    coord.hass = MagicMock()
    coord.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    return coord


def _checkpoint_force(coord):
    """The force= value the checkpoint handed run_optimizer."""
    return coord.run_optimizer.call_args.kwargs["force"]


def test_checkpoint_forces_run_when_tomorrow_unplanned():
    """Issue #18: when Emaldo has published tomorrow's prices but the tomorrow
    plan was never built (the slot_count watcher missed the 96->192
    transition), the periodic checkpoint must force a rebuild instead of
    letting _should_reoptimize skip it."""
    coord = _checkpoint_coord()
    coord._last_result_tomorrow = None
    coord._has_tomorrow_prices = MagicMock(return_value=True)

    coord._checkpoint_callback(None)

    coord.run_optimizer.assert_called_once()
    assert coord.run_optimizer.call_args.kwargs["reason"] == "checkpoint"
    assert _checkpoint_force(coord) is True


def test_checkpoint_keeps_force_false_when_tomorrow_plan_exists():
    coord = _checkpoint_coord()
    coord._last_result_tomorrow = object()  # truthy — already planned
    coord._has_tomorrow_prices = MagicMock(return_value=True)

    coord._checkpoint_callback(None)

    assert _checkpoint_force(coord) is False


def test_checkpoint_keeps_force_false_without_tomorrow_prices():
    coord = _checkpoint_coord()
    coord._last_result_tomorrow = None
    coord._has_tomorrow_prices = MagicMock(return_value=False)

    coord._checkpoint_callback(None)

    assert _checkpoint_force(coord) is False


def test_setup_listeners_emaldo_source_does_not_crash():
    """Issue #20: the Emaldo price-source branch used to pass
    attribute="slot_count" to async_track_state_change_event, which does not
    accept it — TypeError at async_setup_entry, whole integration fails to
    load.  Setup with the Emaldo source must succeed and register exactly one
    price watcher (no duplicate attribute tracker)."""
    from custom_components.battery_optimizer import const as _const
    import custom_components.battery_optimizer.coordinator as _co

    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._unsub_listeners = []
    coord._ha_started = True
    coord._unsub_ha_started = None
    coord.hass = MagicMock()
    coord._entry = MagicMock()
    coord._entry.data = {_const.CONF_PRICE_SOURCE: _const.PRICE_SOURCE_EMALDO}
    coord._entry.options = {}
    coord._emaldo_entry_id = "entry-x"
    coord._config_int = MagicMock(return_value=120)
    coord._unsub_guard = None
    coord._resolve_emaldo_entity = MagicMock(
        side_effect=lambda key, domain="sensor": f"sensor.emaldo_{key}"
    )
    coord._find_balancing_sensor = MagicMock(return_value=None)
    coord._effective_grid_import_sensor = MagicMock(return_value=None)
    coord._effective_grid_export_sensor = MagicMock(return_value=None)
    coord._schedule_startup_run = MagicMock()
    coord._pv_reconcile_callback = MagicMock()
    coord._capture_cost_slot = MagicMock()
    coord._soc_guard_callback = MagicMock()

    with patch.object(_co, "async_track_time_change", return_value=lambda: None), \
         patch.object(_co, "async_track_time_interval", return_value=lambda: None), \
         patch.object(_co, "async_track_state_change_event") as tracker:
        coord.async_setup_listeners()

    # One watcher per price-source entity (schedule_chart), no attribute= call.
    chart_calls = [c for c in tracker.call_args_list
                   if "emaldo_schedule_chart" in str(c.args)]
    assert len(chart_calls) == 1
    assert "attribute" not in (chart_calls[0].kwargs or {})


def _cutoff_coord(time_zone):
    """Coordinator stub for _precharge_publish_cutoff_slot only."""
    from custom_components.battery_optimizer import const as _const

    coord = object.__new__(BatteryOptimizerCoordinator)
    coord._entry = MagicMock()
    coord._entry.data = {}
    coord._entry.options = {}
    coord.hass = MagicMock()
    coord.hass.config.time_zone = time_zone
    return coord


def test_precharge_publish_cutoff_slot_is_dst_correct():
    """The publish cutoff is the local slot of Nord Pool's 13:00
    Europe/Stockholm publish, DST-correct for any market.  A hardcoded
    easternmost-market slot (56) would only fit Finland and would drift by
    one slot across DST."""
    from zoneinfo import ZoneInfo

    # Winter: 13:00 CET (Stockholm) == 12:00 UTC == slot 48.
    winter = datetime(2026, 1, 15, 10, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    with patch("homeassistant.util.dt.now", return_value=winter):
        assert _cutoff_coord("UTC")._precharge_publish_cutoff_slot() == 48
        # Finland: 13:00 CET == 14:00 EET == slot 56.
        assert _cutoff_coord("Europe/Helsinki")._precharge_publish_cutoff_slot() == 56

    # Summer: 13:00 CEST (Stockholm) == 11:00 UTC == slot 44.
    summer = datetime(2026, 7, 15, 10, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    with patch("homeassistant.util.dt.now", return_value=summer):
        assert _cutoff_coord("UTC")._precharge_publish_cutoff_slot() == 44
        assert _cutoff_coord("Europe/Helsinki")._precharge_publish_cutoff_slot() == 56


def test_precharge_publish_cutoff_slot_override_takes_precedence():
    """precharge_publish_hour (local hour) overrides the derived cutoff."""
    coord = _cutoff_coord("Europe/Helsinki")
    coord._entry.data = {"precharge_publish_hour": 15.5}
    # 15.5 h == 15:30 == slot 62.
    assert coord._precharge_publish_cutoff_slot() == 62