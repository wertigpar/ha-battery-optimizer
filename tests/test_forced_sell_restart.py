"""Forced-sell restart persistence (issue #30).

A restart during a forced-sell window loses the in-memory
``_manual_selling_active`` flag.  Every stop path then treats the switch as
not ours, so no ``turn_off`` is sent and the sale runs on unmanaged until
Emaldo's own staged target runs out - 8 minutes past the window and SoC
61 -> 41 % in the reported case.

The decision table itself is unit-tested in tests/test_sale_state.py.  This
file covers the WIRING: that the marker is written when the sale starts,
cleared when it stops, and that the reconciliation runs before the start
path so a recovered sale cannot be duplicated.

The first half is structural (the AST checks below).  The second half is
functional: the reconciliation is driven on a real coordinator instance and
asserted on its EFFECT, because that effect - a sale re-adopted, a window
recorded as served, a sale stopped - is the whole point of the change.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from . import load_coordinator, load_sale_state

coord, const = load_coordinator()
sale_state, _ = load_sale_state()

_COORD_SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "battery_optimizer" / "coordinator.py"
)


def _tree() -> ast.Module:
    return ast.parse(_COORD_SRC.read_text(encoding="utf-8"))


def _class_body(name: str) -> ast.ClassDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _methods(name: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        n.name: n
        for n in _class_body(name).body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imports_module(tree: ast.Module, name: str) -> bool:
    """True when the source imports ``name`` out of its own package.

    The package's pure modules are imported two ways and both count:
    ``from . import marker_io`` (module-attribute access) and
    ``from .marker_io import atomic_write_json`` (direct names).

    The first form parses as ``ImportFrom(module=None, level=1, ...)`` - the
    module name lives in the alias, not in ``node.module`` - so a plain
    ``node.module == name`` test silently misses it and the assertion would
    pass for the wrong reason (or fail for the right one by accident).
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module == name:
            return True
        if node.module is None and node.level == 1:
            if any(alias.name == name for alias in node.names):
                return True
    return False


def _mentions(name: str, method: ast.AST) -> list[ast.Attribute]:
    """Every ``self.<name>`` reference inside ``method``.

    Matches the bare attribute reference as well as a direct call: the marker
    helpers are handed to ``async_add_executor_job`` as callables (the file
    I/O must stay off the event loop), so in the source they are references,
    not call nodes.  A reference that does not exist yields an empty list, so
    the assertion stays non-vacuous.
    """
    return [
        n
        for n in ast.walk(method)
        if isinstance(n, ast.Attribute) and n.attr == name
    ]


def test_coordinator_uses_the_shared_marker_io():
    """Both markers must go through marker_io, not their own copy of the I/O."""
    assert _imports_module(_tree(), "marker_io"), (
        "coordinator does not import marker_io"
    )


def test_coordinator_imports_sale_state():
    assert _imports_module(_tree(), "sale_state"), (
        "coordinator does not import sale_state"
    )


def test_sale_marker_is_written_when_the_sale_starts():
    """The write must sit with the flag being set, not before the turn_on."""
    methods = _methods("BatteryOptimizerCoordinator")
    start = methods["_manage_manual_selling_locked"]
    writes = _mentions("_write_sale_marker", start)
    assert writes, "the sale marker is never written"
    active = [
        n
        for n in ast.walk(start)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute) and t.attr == "_manual_selling_active"
            for t in n.targets
        )
    ]
    assert active, "_manual_selling_active is never assigned"
    assert writes[0].lineno > active[0].lineno, (
        "the marker must be written AFTER _manual_selling_active is set - the "
        "sale is only ours once the switch is verified on"
    )
    # And after the read-back that proves the sale is OBSERVED running: a start
    # Emaldo refuses does not raise, so a marker written before that read-back
    # would exist for a sale that never ran, and the recovery would consume the
    # window as if it had been served.
    readback = _mentions("_manual_selling_is_on", start)
    assert readback, "the _manual_selling_is_on read-back is missing"
    assert writes[0].lineno > readback[0].lineno, (
        "the marker must be written AFTER the _manual_selling_is_on read-back - "
        "only an observed-running sale may leave a marker"
    )
    # The helper is what actually writes: the payload is built and handed to
    # the file writer there, so the window arithmetic lives in ONE place for
    # both observation sites.
    helper = ast.dump(methods["_write_sale_marker"])
    assert "build_sale_marker" in helper
    assert "_write_manual_sell_marker" in helper


def test_sale_marker_is_cleared_on_the_stop_path():
    methods = _methods("BatteryOptimizerCoordinator")
    stop = methods["_stop_manual_selling_locked"]
    clears = _mentions("_clear_manual_sell_marker", stop)
    assert clears, "the stop path never clears the sale marker"


def test_reconciliation_runs_before_the_start_path():
    """A recovered sale must be re-adopted before the start path can open one."""
    methods = _methods("BatteryOptimizerCoordinator")
    manage = methods["_manage_manual_selling_locked"]
    reconcile = _mentions("_reconcile_manual_selling_marker_locked", manage)
    assert reconcile, "the marker is never reconciled in the sale-management path"
    guard = [
        n
        for n in ast.walk(manage)
        if isinstance(n, ast.If)
        and "_manual_selling_active" in ast.dump(n.test)
    ]
    assert guard, "the already-running guard is missing"
    assert reconcile[0].lineno < guard[0].lineno, (
        "the reconciliation must run before the already-running guard, or a "
        "recovered sale can be opened a second time"
    )


def test_recovery_uses_the_pure_decision():
    """The decision must come from sale_state, not be re-implemented here."""
    methods = _methods("BatteryOptimizerCoordinator")
    reconcile = methods["_reconcile_manual_selling_marker_locked"]
    body = ast.dump(reconcile)
    assert "decide_sale_recovery" in body, (
        "the reconciliation does not call sale_state.decide_sale_recovery"
    )
    assert "build_sale_marker" in ast.dump(
        methods["_write_sale_marker"]
    ), "the marker is not built by sale_state.build_sale_marker"


def test_sale_marker_is_scoped_to_the_config_entry():
    """Two entries on one Emaldo device must not clear each other's marker."""
    methods = _methods("BatteryOptimizerCoordinator")
    name = methods["_manual_sell_marker_name"]
    body = ast.dump(name)
    assert "entry_id" in body, "the marker filename is not scoped by entry id"
    assert "battery_optimizer_manual_sell" in body, (
        "unexpected marker filename"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Functional coverage: drive the reconciliation and assert on its EFFECT.
#
# The AST checks above pin the wiring; these pin what the wiring is FOR.
# The fixture pattern (a coordinator built with __new__, an inline executor,
# a fake hass) is the one already used by tests/test_forced_sell_tick.py.
# ─────────────────────────────────────────────────────────────────────────────

TZ = timezone(timedelta(hours=3))
WINDOW = (80, 81)  # 20:00-20:30, two 15-minute slots


def _now(hour: int = 20, minute: int = 0) -> datetime:
    return datetime(2026, 10, 20, hour, minute, tzinfo=TZ)


def _window_end(window=WINDOW) -> datetime:
    """The start of the slot after the window's last one - the instant the
    coordinator's own window rule stops the window."""
    return datetime(2026, 10, 20, 0, 0, tzinfo=TZ) + timedelta(
        minutes=15 * (window[-1] + 1)
    )


def _marker_payload(window=WINDOW, started=None) -> dict:
    started = started or _now()
    return sale_state.build_sale_marker(
        started, list(window), _window_end(window)
    )


def _write_marker(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _switch_state_until_stopped(state: str = "on"):
    """hass.states.get stub whose state flips when a turn_off is issued.

    Models the device: the command takes effect and the entity state
    follows.  The state is left alone when the command does NOT take
    effect - which is what Emaldo's switch does when the device ignores
    the stop, and the case the read-back exists for.
    """
    holder = {"state": state}

    def _get(entity_id):
        obj = MagicMock()
        obj.state = holder["state"]
        return obj

    get = MagicMock(side_effect=_get)
    get.holder = holder
    return get


def _switch_state(state: str):
    """hass.states.get stub returning a real on/off state."""
    obj = MagicMock()
    obj.state = state
    return MagicMock(return_value=obj)


def _honouring_stop(c):
    """Make the coordinator's turn_off take effect.

    The switch reads off once the command has been issued, which is what a
    device that honours the stop does - and what the read-back that
    confirms the stop expects.
    """
    switch = _switch_state_until_stopped("on")
    c.hass.states.get = switch

    async def _call(domain, service, *args, **kwargs):
        if service == "turn_off":
            switch.holder["state"] = "off"

    c.hass.services.async_call = AsyncMock(side_effect=_call)
    return c


def _start_coord(marker_path, *, switch_state: str = "on"):
    """A coordinator that can run the REAL start path - so it writes the
    marker - and the real stop path."""
    c = _coord(marker_path, switch_state=switch_state)
    c._emaldo_control_enabled = True
    c._forced_sell_lock = asyncio.Lock()
    c._sell_back_to_grid_switch_id_cached = None
    c.resolve_emaldo_device = MagicMock(return_value=True)
    c._manual_selling_started_at = None
    c._manual_selling_start_attempts = 0
    c._manual_selling_start_window = None
    return c


def _result(window=WINDOW, target=5.0):
    """A plan with a sell window, as the optimizer hands it over."""
    result = MagicMock()
    result.sell_slots = list(window)
    result.sell_target_kwh = target
    return result


def _sell_back_coord(
    tmp_path, *, manual_state: str = "on", sale_marker: str = "real"
):
    """A coordinator with the sell-back marker on disk - as a crashed
    session leaves it - and a sale marker beside it.

    ``_last_result`` is None on purpose: right after a reboot there is no
    plan yet, which is exactly the state a reboot during a sell window
    leaves behind, and the state the plan-based window check cannot see
    through.

    ``sale_marker`` picks what the sale marker's file holds: "real" (a sale
    of ours was running), "unreadable" (a corrupt file), "quarantined" (the
    empty-window payload written over an undeletable file) or "absent".
    """
    c = coord.BatteryOptimizerCoordinator.__new__(
        coord.BatteryOptimizerCoordinator
    )
    c._manual_sell_state_path = str(tmp_path / "manual_sell.json")
    c._manual_sell_reconciled = False
    c._manual_selling_active = False
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    c._sell_back_to_grid_saved = None
    c._sell_back_to_grid_restored = False
    c._sell_back_to_grid_state_path = str(tmp_path / "sell_back.json")
    c._sell_back_to_grid_switch_id_cached = None
    c._forced_sell_lock = asyncio.Lock()
    c._last_result = None
    c._resolve_emaldo_entity = MagicMock(
        side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}"
    )
    c.hass = MagicMock()
    c.hass.async_add_executor_job = _inline_executor()
    c.hass.services.async_call = AsyncMock()
    c.hass.states.get = _switch_state(manual_state)
    # A confirmed stop queues a re-plan (issue #30); close the coroutine
    # instead of leaving it un-awaited on a Mock.
    c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    # The sell-back marker as a crashed session leaves it: we enabled it.
    (tmp_path / "sell_back.json").write_text(
        json.dumps({"we_enabled": True}), encoding="utf-8"
    )
    if sale_marker == "real":
        _write_marker(tmp_path / "manual_sell.json", _marker_payload())
    elif sale_marker == "unreadable":
        (tmp_path / "manual_sell.json").write_text(
            "{not json", encoding="utf-8"
        )
    elif sale_marker == "quarantined":
        (tmp_path / "manual_sell.json").write_text(
            json.dumps(coord._QUARANTINED_SALE_MARKER), encoding="utf-8"
        )
    return c


def _inline_executor():
    """hass.async_add_executor_job, run on the spot - unit test, not HA."""
    async def job(func, *args):
        return func(*args)
    return job


def _coord(marker_path, *, switch_state: str = "on"):
    """A coordinator carrying only the state the reconciliation reads."""
    c = coord.BatteryOptimizerCoordinator.__new__(
        coord.BatteryOptimizerCoordinator
    )
    c._manual_sell_state_path = str(marker_path)
    c._manual_sell_reconciled = False
    c._manual_selling_active = False
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    c._manual_selling_started_at = None
    c._manual_selling_start_window = None
    c._manual_selling_switch_id_cached = None
    c._manual_selling_ours = False
    c._sell_back_to_grid_saved = None
    c._resolve_emaldo_entity = MagicMock(
        side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}"
    )
    c.hass = MagicMock()
    c.hass.async_add_executor_job = _inline_executor()
    c.hass.services.async_call = AsyncMock()
    c.hass.states.get = _switch_state(switch_state)
    c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    return c


def test_reconcile_adopts_a_running_sale_inside_the_window(tmp_path):
    """A sale that was running when the process died is re-adopted.

    Without this the start path would see no active sale and open a SECOND
    one for the same window - the double export.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is True
    assert c._manual_selling_seen_on is True
    # The date is a DATE, not the ISO string the marker carries: the served
    # key is compared against (now.date(), tuple(window)).
    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW)
    assert isinstance(c._manual_selling_served[0], date)
    # The sale is still running - the recovery itself turns nothing off.
    c.hass.services.async_call.assert_not_awaited()


def test_the_adopt_branch_does_not_latch(tmp_path):
    """The adopt branch must leave the latch DOWN.

    The marker is still on disk - the sale is still running - so latching
    here would make the marker unreadable for the rest of the session, and
    RECOVER_STOP, the only path that turns an adopted sale off, could then
    never fire.  The latch may only go down when the marker is gone.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is True
    assert c._manual_sell_reconciled is False, (
        "the latch may only go down when the marker is gone"
    )
    assert marker.exists(), "an adopted sale must keep its marker"


def test_recover_stop_fires_on_a_later_reconcile(tmp_path):
    """RECOVER_STOP has to stay reachable for an adopted sale.

    The adopt branch leaves the marker on disk, so the reconcile keeps
    running; once the window has closed, the next reconcile must turn the
    sale off.  This is the only path that issues a turn_off for an adopted
    sale, and it is what stops the orphan of issue #30 when there is no
    plan to drive the manage path.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")
    _honouring_stop(c)

    # First pass: inside the window -> the sale is adopted, nothing is off.
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))
    assert c._manual_selling_active is True
    c.hass.services.async_call.assert_not_awaited()

    # Second pass: the window has closed -> the sale must be stopped.
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(21, 0)))
    c.hass.services.async_call.assert_awaited_once()
    assert c.hass.services.async_call.await_args[0][:2] == (
        "switch",
        "turn_off",
    )
    assert not marker.exists(), "a stopped sale must not leave a marker"
    assert c._manual_selling_active is False


def test_reconcile_stamps_the_window_when_the_sale_finished_on_its_own(tmp_path):
    """The switch reads off: the sale finished by itself.

    The completion path could not stamp the window as served - it returns
    early when the sale is not active, which is exactly the state a restart
    leaves.  Without the stamp the next tick opens a SECOND sale for the
    same window.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="off")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW)
    assert c._manual_selling_active is False
    assert not marker.exists(), "a finished sale must not leave a marker"
    c.hass.services.async_call.assert_not_awaited()


def test_reconcile_stops_a_sale_whose_window_has_closed(tmp_path):
    """Past the window: re-adopt so the stop path can interrupt the sale
    instead of taking its early return."""
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")
    _honouring_stop(c)

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(21, 0)))

    c.hass.services.async_call.assert_awaited_once()
    assert c.hass.services.async_call.await_args[0][:2] == (
        "switch",
        "turn_off",
    )
    assert not marker.exists(), "a stopped sale must not leave a marker"
    assert c._manual_selling_active is False


def test_reconcile_leaves_an_unreadable_marker_alone(tmp_path):
    """A corrupt marker is NOT 'nothing to restore'.

    Nothing may change and the marker must survive, so the next tick can
    retry instead of silently treating a corrupt file as 'no sale'.
    """
    marker = tmp_path / "marker.json"
    marker.write_text("{not json", encoding="utf-8")
    c = _coord(marker, switch_state="on")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is False
    assert c._manual_selling_seen_on is False
    assert c._manual_selling_served is None
    assert marker.exists(), "an unreadable marker must not be cleared"
    assert c._manual_sell_reconciled is False, "it must be retried"
    c.hass.services.async_call.assert_not_awaited()


def test_latch_is_not_set_for_an_unparseable_marker(tmp_path):
    """A marker that parses as JSON but is not a usable marker.

    The latch may only go down when the marker is GONE: latching here would
    switch the retry off for the rest of the session, so a marker that
    becomes readable again later would never be noticed.
    """
    marker = tmp_path / "marker.json"
    marker.write_text(
        json.dumps(
            {
                "started_at": "2026-10-20T20:00:00+03:00",
                "window_end": "2026-10-20T20:30:00+03:00",
                "served_date": "not-a-date",
                "served_window": [80, 81],
            }
        ),
        encoding="utf-8",
    )
    c = _coord(marker, switch_state="on")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is False
    assert c._manual_selling_seen_on is False
    assert c._manual_selling_served is None
    assert marker.exists(), "an unusable marker must not be cleared"
    assert c._manual_sell_reconciled is False, "it must be retried"
    c.hass.services.async_call.assert_not_awaited()


def test_latch_is_not_set_for_an_unknown_switch_state(tmp_path):
    """The switch state cannot be read.

    Nothing may be guessed - the sale may be running or finished - so
    nothing changes, the marker survives and the latch stays down, so a
    later tick tries again.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="unavailable")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is False
    assert c._manual_selling_seen_on is False
    assert c._manual_selling_served is None
    assert marker.exists(), "an unknown switch state must not clear the marker"
    assert c._manual_sell_reconciled is False, "it must be retried"
    c.hass.services.async_call.assert_not_awaited()


def test_reconcile_with_no_marker_changes_nothing(tmp_path):
    """No marker at all: nothing to restore, and no error."""
    marker = tmp_path / "marker.json"  # never written
    c = _coord(marker, switch_state="on")

    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert c._manual_selling_active is False
    assert c._manual_selling_seen_on is False
    assert c._manual_selling_served is None
    assert not marker.exists()
    c.hass.services.async_call.assert_not_awaited()


# ── the marker-clear rule: only a CONFIRMED stop clears the marker ─────────


def _stopping_coord(marker_path, *, switch_state: str = "on"):
    """A coordinator with an active sale and a marker on disk."""
    c = _coord(marker_path, switch_state=switch_state)
    c._manual_selling_active = True
    return c


def test_a_failed_turn_off_keeps_the_marker(tmp_path):
    """The marker must survive a failed stop.

    It is the only record that a later restart has to turn the sale off; a
    marker cleared by a failed turn_off re-creates the very orphan the
    marker exists to prevent.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")
    c.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))

    asyncio.run(c._stop_manual_selling_locked())

    assert marker.exists(), (
        "a failed turn_off must leave the marker: a later restart needs it"
    )
    assert c._manual_selling_active is False


def test_a_timed_out_turn_off_keeps_the_marker(tmp_path):
    """The timeout branch is the same shape as the failure branch."""
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")

    async def _slow_call(*args, **kwargs):
        await asyncio.sleep(5)

    c.hass.services.async_call = AsyncMock(side_effect=_slow_call)
    with patch.object(coord, "_MANUAL_SELLING_STOP_TIMEOUT_S", 0.01):
        asyncio.run(c._stop_manual_selling_locked())

    assert marker.exists(), (
        "a timed-out turn_off must leave the marker: a later restart needs it"
    )


def test_a_successful_turn_off_removes_the_marker(tmp_path):
    """A CONFIRMED stop clears the marker - the invariant the failed-stop
    branch protects.

    The confirmation is the READ-BACK, not the service call returning: the
    command is accepted here, the device honours it, and the switch reads
    off afterwards.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")
    switch = _switch_state_until_stopped("on")
    c.hass.states.get = switch

    async def _call(domain, service, *args, **kwargs):
        if service == "turn_off":
            switch.holder["state"] = "off"

    c.hass.services.async_call = AsyncMock(side_effect=_call)

    asyncio.run(c._stop_manual_selling_locked())

    c.hass.services.async_call.assert_awaited_once()
    assert not marker.exists(), "a confirmed stop must clear the marker"
    assert c._manual_selling_active is False


def test_a_stop_that_does_not_take_effect_keeps_the_marker(tmp_path):
    """The service call returning is NOT a confirmation.

    Emaldo's switch never raises when the device ignores the command - it
    logs a warning and returns the last confirmed value - so a turn_off
    that "succeeded" can leave the sale running.  The marker must survive:
    it is the only record a later restart has.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")
    # The command is accepted, but the device keeps selling: the switch
    # still reads on afterwards.
    c.hass.services.async_call = AsyncMock()

    asyncio.run(c._stop_manual_selling_locked())

    c.hass.services.async_call.assert_awaited_once()
    assert marker.exists(), (
        "a stop that did not take effect must leave the marker: the sale may "
        "still be running"
    )
    assert c._manual_selling_active is False


def _switch_state_stale_then_off(state: str = "on", stale_reads: int = 2):
    """hass.states.get stub: reads `state` for the first `stale_reads` reads
    and off afterwards.

    Models a state machine that has not caught up with the device yet - the
    case the self-healing claim rests on.
    """
    holder = {"reads": 0}

    def _get(entity_id):
        holder["reads"] += 1
        obj = MagicMock()
        obj.state = state if holder["reads"] <= stale_reads else "off"
        return obj

    get = MagicMock(side_effect=_get)
    get.holder = holder
    return get


def test_a_sale_observed_running_later_writes_the_marker(tmp_path):
    """The start read-back may not have seen "on" yet.

    The device can need a moment, and a later tick that observes the sale
    running must write the marker too - otherwise a restart in between
    orphans the sale.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="on")
    c._manual_selling_active = True
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    c._manual_selling_started_at = _now(20, 0)
    c._manual_selling_start_window = (date(2026, 10, 20), WINDOW)

    asyncio.run(
        c._check_manual_selling_self_completion_locked(_result(), _now(20, 0))
    )

    assert marker.exists(), "an observed-running sale must leave a marker"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["served_window"] == list(WINDOW)
    assert payload["served_date"] == "2026-10-20"
    assert c._manual_selling_seen_on is True
    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW)


def test_an_observed_sale_keeps_its_own_window_when_the_plan_lost_its(tmp_path):
    """The current plan's window is not the sale's window.

    A plan that lost its sell window must not leave a running sale without a
    marker (that is the #30 orphan), and the marker must name the window the
    sale was actually started for - the one that must not be sold twice.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="on")
    c._manual_selling_active = True
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    # The sale was started for WINDOW; the plan has since lost its window.
    c._manual_selling_started_at = _now(20, 0)
    c._manual_selling_start_window = (date(2026, 10, 20), WINDOW)

    asyncio.run(
        c._check_manual_selling_self_completion_locked(
            _result(window=()), _now(20, 5)
        )
    )

    assert marker.exists(), "a running sale must always have a marker"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["served_window"] == list(WINDOW)
    assert payload["served_date"] == "2026-10-20"
    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW)


def test_a_sale_that_already_stopped_clears_the_marker(tmp_path):
    """The switch reads off: there is nothing to interrupt, and the sale is
    confirmed stopped, so the marker goes."""
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="off")

    asyncio.run(c._stop_manual_selling_locked())

    assert not marker.exists()
    c.hass.services.async_call.assert_not_awaited()


def test_a_stale_read_after_a_successful_stop_is_self_healing(tmp_path):
    """The read-back can still read "on" right after a successful stop.

    The marker lingers for that tick, and the next reconcile reads the
    settled state and clears it - that is the self-healing the code claims,
    and it is what makes the read-back safe to rely on.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")
    # The command is accepted, but the state machine has not caught up yet:
    # the first two reads still say "on".
    c.hass.states.get = _switch_state_stale_then_off()
    c.hass.services.async_call = AsyncMock()

    asyncio.run(c._stop_manual_selling_locked())

    c.hass.services.async_call.assert_awaited_once()
    assert marker.exists(), "a stale read must not clear the marker"

    # The next reconcile reads the settled state and clears it.
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))
    assert not marker.exists(), "the stale read must be self-healing"


# ── the latch re-arm: a marker written mid-session ────────────────────────


def test_a_marker_written_in_the_session_re_arms_the_latch(tmp_path):
    """The whole sequence of the review's Important 1.

    The session starts with no marker, so the first reconcile latches.  A
    sale starts later and writes a marker - the latch has to go down again,
    or a stop whose turn_off fails would leave the marker on disk with the
    reconcile latched, and the sale would never be re-adopted or retried.
    """
    marker = tmp_path / "marker.json"
    c = _start_coord(marker, switch_state="on")
    # No marker at session start -> the first reconcile settles the latch.
    c._manual_sell_reconciled = True
    switch = _switch_state_until_stopped("on")
    c.hass.states.get = switch
    fail = {"now": True}

    async def _call(domain, service, *args, **kwargs):
        if service == "turn_off":
            if fail["now"]:
                raise RuntimeError("boom")
            switch.holder["state"] = "off"

    c.hass.services.async_call = AsyncMock(side_effect=_call)

    # A sale starts: the marker is written, and the latch must go down.
    asyncio.run(c._manage_manual_selling(_result(), _now(20, 0)))
    assert marker.exists(), "the start must write the marker"
    assert c._manual_sell_reconciled is False, (
        "a marker written in the session must re-arm the reconcile"
    )

    # The window-end stop fails: the sale may still be running, so the
    # marker survives and the in-memory flag is cleared.
    asyncio.run(c._stop_manual_selling_locked())
    assert marker.exists(), "a failed turn_off must leave the marker"
    assert c._manual_selling_active is False

    # The next reconcile must re-adopt the sale and retry the stop - and
    # this time the device honours it, so the sale is confirmed stopped and
    # the marker goes.
    fail["now"] = False
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(21, 0)))
    c.hass.services.async_call.assert_awaited()
    assert c.hass.services.async_call.await_args[0][:2] == (
        "switch",
        "turn_off",
    )
    assert not marker.exists(), "a confirmed stop must not leave a marker"
    assert c._manual_selling_active is False


# ── the two recoveries must not fight over the prerequisite ───────────────


def test_the_sell_back_restore_leaves_the_prerequisite_alone_while_a_sale_runs(
    tmp_path,
):
    """A running sale needs the Sell back to grid prerequisite.

    After a reboot there is no plan, so the plan-based window check cannot
    see the live window - and without the sale marker the restore would
    withdraw the prerequisite out from under the running sale, which is
    exactly what the sibling's own comment forbids.
    """
    c = _sell_back_coord(tmp_path, manual_state="on")

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_not_awaited()
    assert c._sell_back_to_grid_restored is False, (
        "the restore must keep retrying while the sale runs"
    )


def test_the_sell_back_restore_withdraws_the_prerequisite_once_the_sale_is_over(
    tmp_path,
):
    """The switch reads off: the sale is over, so the stranded prerequisite
    is put back the user's way."""
    c = _sell_back_coord(tmp_path, manual_state="off")

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_awaited_once()
    assert c.hass.services.async_call.await_args[0][:2] == (
        "switch",
        "turn_off",
    )
    assert c._sell_back_to_grid_restored is True


def test_the_sell_back_restore_leaves_the_prerequisite_alone_for_a_doubtful_marker(
    tmp_path,
):
    """A corrupt marker is DOUBT, and doubt must not be the thing that acts.

    Withdrawing the prerequisite under a possibly running sale would make
    Emaldo abort it, so the restore changes nothing and keeps retrying -
    the house rule for an unreadable state.
    """
    c = _sell_back_coord(
        tmp_path, manual_state="on", sale_marker="unreadable"
    )

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_not_awaited()
    assert c._sell_back_to_grid_restored is False, (
        "the restore must keep retrying while the state is doubtful"
    )


def test_the_sell_back_restore_proceeds_for_a_quarantined_marker(tmp_path):
    """A quarantined marker says our sale was CONFIRMED stopped.

    The restore must proceed even though the *Manual selling* switch still
    reads "on": the "on" is stale, or it is another entry's sale.  Without
    this the prerequisite would stay on forever and the restore would retry
    once per tick.
    """
    c = _sell_back_coord(
        tmp_path, manual_state="on", sale_marker="quarantined"
    )

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_awaited_once()
    assert c.hass.services.async_call.await_args[0][:2] == (
        "switch",
        "turn_off",
    )
    assert c._sell_back_to_grid_restored is True


def test_the_sell_back_restore_leaves_the_prerequisite_alone_when_the_switch_is_unknown(
    tmp_path,
):
    """Our own marker says a sale of ours was running, and nothing confirms
    that it ended.

    Right after a reboot the Emaldo entities are not there yet, so the
    switch state cannot be read - and that is exactly when the sale marker
    exists.  An unknown switch state is not a confirmation.
    """
    c = _sell_back_coord(tmp_path, manual_state="unavailable")

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_not_awaited()
    assert c._sell_back_to_grid_restored is False


def test_the_sell_back_restore_leaves_the_prerequisite_alone_while_the_switch_reads_on(
    tmp_path,
):
    """No sale of ours - but the *Manual selling* switch reads "on".

    Something is selling, and the prerequisite is exactly what a manual sale
    needs, so it stays - whatever our own marker says, or does not say.
    """
    c = _sell_back_coord(tmp_path, manual_state="on", sale_marker="absent")

    asyncio.run(c._restore_sell_back_to_grid_after_restart())

    c.hass.services.async_call.assert_not_awaited()
    assert c._sell_back_to_grid_restored is False


# ── the quarantine for a marker that cannot be deleted ─────────────────────


def test_an_undeletable_marker_is_quarantined(tmp_path):
    """A marker that cannot be deleted must not be re-decided on every boot.

    The delete is attempted first, so a transient failure is still retried
    (the next clear attempt removes the file for real).  When the delete
    fails the marker is overwritten with a payload that means "nothing to
    restore", and the reconcile recognises it and decides nothing.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")

    with patch.object(coord.os, "remove", side_effect=OSError("busy")):
        c._clear_manual_sell_marker()

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["served_window"] == [], "the marker must be quarantined"
    assert payload["served_date"] == "1970-01-01"

    # And the reconcile decides nothing about a quarantined marker.
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))
    assert c._manual_selling_active is False
    assert c._manual_selling_seen_on is False
    assert c._manual_selling_served is None
    assert c._manual_sell_reconciled is True, (
        "a quarantined marker settles the reconcile"
    )
    c.hass.services.async_call.assert_not_awaited()


# ── the sale's own window: the served stamp and the marker's end ──────────


def test_the_served_stamp_names_the_sale_own_window_after_the_plan_moved(
    tmp_path,
):
    """The stamp must survive a plan that moved mid-sale.

    Otherwise the window the sale was actually for loses its "already
    served" protection, and a later plan that returns to that exact window
    can open a second sale for it - the same double-export class this
    branch exists to kill.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="off")
    c._manual_selling_active = True
    c._manual_selling_seen_on = True
    c._manual_selling_served = None
    c._manual_selling_started_at = _now(20, 0)
    c._manual_selling_start_window = (date(2026, 10, 20), WINDOW)
    # The plan has since moved to a different window.
    moved = _result(window=(40, 41))

    # The sale finishes on its own: the switch reads off.
    asyncio.run(
        c._check_manual_selling_self_completion_locked(moved, _now(20, 5))
    )

    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW), (
        "the served stamp must name the window the sale was started for"
    )


def test_an_adopted_sale_keeps_its_own_window_when_the_plan_moved(tmp_path):
    """The reviewer's reproduction: adopt, move the plan, let the sale end.

    An adopted sale was started BEFORE the restart, so it has no start
    window of its own - the reconcile restored only the served key.  The
    self-completion path then stamped the served key from the CURRENT
    plan's window, which is not the window the sale sold: the sold window
    lost its "already served" protection, and a later plan returning to it
    could open a second sale for it.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")

    # Tick 1: the reconcile adopts the running sale.
    asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))
    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW)
    assert c._manual_selling_start_window == (date(2026, 10, 20), WINDOW), (
        "the adopt branch must restore the sale's own window, not just its "
        "served key"
    )

    # The plan moves to a different window while the sale runs.
    moved = _result(window=(82, 83))

    # Tick N: the sale finishes on its own - the switch reads off.
    c.hass.states.get = _switch_state("off")
    asyncio.run(
        c._check_manual_selling_self_completion_locked(moved, _now(20, 5))
    )

    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW), (
        "the served stamp must name the window the sale was started for, "
        "not the plan's"
    )


def test_the_completion_keeps_the_served_window_when_the_plan_has_none(
    tmp_path,
):
    """An empty served key matches no real window.

    The plan can lose its sell window while a sale runs, and the fallback
    would then stamp an empty window - which matches nothing, so the
    double-export guard would be silently inert.  The previous value is kept
    instead.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="off")
    c._manual_selling_active = True
    c._manual_selling_seen_on = True
    c._manual_selling_served = (date(2026, 10, 20), WINDOW)
    c._manual_selling_started_at = _now(20, 0)
    # The sale's own window is genuinely unknown.
    c._manual_selling_start_window = None

    asyncio.run(
        c._check_manual_selling_self_completion_locked(
            _result(window=()), _now(20, 5)
        )
    )

    assert c._manual_selling_served == (date(2026, 10, 20), WINDOW), (
        "an empty served window matches no real window; keep the previous one"
    )


def test_the_window_end_is_on_the_sale_own_day(tmp_path):
    """A sale started late in the evening is closed on ITS day.

    The marker's window_end is the start of the slot after the window's last
    one, on the day the WINDOW belongs to - the day the sale was started.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="on")
    c._manual_selling_active = True
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    started = datetime(2026, 10, 20, 22, 0, tzinfo=TZ)
    c._manual_selling_started_at = started
    c._manual_selling_start_window = (date(2026, 10, 20), (88, 89))

    asyncio.run(
        c._check_manual_selling_self_completion_locked(
            _result(window=(88, 89)), datetime(2026, 10, 20, 22, 5, tzinfo=TZ)
        )
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["window_end"] == "2026-10-20T22:30:00+03:00"
    assert payload["served_date"] == "2026-10-20"


def test_the_window_end_of_a_midnight_spanning_sale_is_just_after_midnight(
    tmp_path,
):
    """A sale that reaches midnight is closed just after midnight.

    The window_end follows the window's own day, not the day the sale was
    OBSERVED: a sale started at 23:50 for the day's last slots is closed at
    00:00, not a day later.  (The live rule stops a sale at the day's end
    anyway, so this is the narrow case where the observation lands on the
    next day - and a window_end a day late would make the recovery ADOPT
    the sale instead of stopping it.)
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="on")
    c._manual_selling_active = True
    c._manual_selling_seen_on = False
    c._manual_selling_served = None
    c._manual_selling_started_at = datetime(2026, 10, 20, 23, 50, tzinfo=TZ)
    c._manual_selling_start_window = (date(2026, 10, 20), (94, 95))

    asyncio.run(
        c._check_manual_selling_self_completion_locked(
            _result(window=(94, 95)), datetime(2026, 10, 21, 0, 5, tzinfo=TZ)
        )
    )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["window_end"] == "2026-10-21T00:00:00+03:00", (
        "the window_end must be just after midnight, on the window's own day"
    )
    assert payload["served_date"] == "2026-10-20"


# ── the reconcile's log: a fresh re-adoption is the only news ────────────


def test_a_sale_this_session_started_is_not_logged_as_re_adopted(
    tmp_path, caplog
):
    """The reconcile runs on every tick, so only a fresh re-adoption is news.

    A sale this session started (or already adopted) must not be logged as
    "re-adopted across the restart" - that would be wrong, and it would put
    an INFO line on every tick for the life of the sale.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")
    # This session owns the sale: it started it.
    c._manual_selling_ours = True

    with caplog.at_level(logging.INFO, logger="bopt_pkg.coordinator"):
        asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert "Re-adopted" not in caplog.text
    assert c._manual_selling_active is True, "the sale is still adopted"


def test_a_sale_adopted_after_a_restart_is_logged_as_re_adopted(
    tmp_path, caplog
):
    """The other half: a sale this session did NOT start IS a re-adoption."""
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _coord(marker, switch_state="on")
    # A fresh process: nothing of ours yet.
    assert c._manual_selling_ours is False

    with caplog.at_level(logging.INFO, logger="bopt_pkg.coordinator"):
        asyncio.run(c._reconcile_manual_selling_marker_locked(_now(20, 0)))

    assert "Re-adopted" in caplog.text


# ── the read-back must inspect the switch that was commanded ─────────────


def _switch_resolver():
    """A resolver that answers the manual-selling switch differently the
    second time it is asked - the entity resolution is deliberately
    uncached, so it CAN change between two calls."""
    state = {"n": 0}

    def _resolve(key, domain="sensor"):
        if key == "manual_selling":
            state["n"] += 1
            return "switch.commanded" if state["n"] == 1 else "switch.other"
        return f"switch.{key}"

    return MagicMock(side_effect=_resolve)


def _switch_states(states: dict[str, str]):
    """hass.states.get stub that answers per entity id."""
    def _get(entity_id):
        obj = MagicMock()
        obj.state = states.get(entity_id, "unavailable")
        return obj

    return MagicMock(side_effect=_get)


def test_the_stop_reads_back_the_switch_it_commanded(tmp_path):
    """The read-back must inspect the switch the stop was sent to.

    The entity resolution is deliberately uncached, so resolving it again
    could land on a different switch - and then the read-back would report
    a state the sale does not have.
    """
    marker = tmp_path / "marker.json"
    _write_marker(marker, _marker_payload())
    c = _stopping_coord(marker, switch_state="on")
    c._resolve_emaldo_entity = _switch_resolver()
    c.hass.states.get = _switch_states(
        {
            "switch.commanded": "on",
            "switch.other": "off",
            # The prerequisite switch has to read a definite state, or the
            # start defers instead of starting.
            "switch.sell_back_to_grid": "on",
        }
    )
    c.hass.services.async_call = AsyncMock()

    asyncio.run(c._stop_manual_selling_locked())

    c.hass.services.async_call.assert_awaited_once()
    assert c.hass.services.async_call.await_args[0][2] == {
        "entity_id": "switch.commanded"
    }
    assert marker.exists(), (
        "the read-back must inspect the commanded switch, not a fresh "
        "resolution of it"
    )


def test_the_start_reads_back_the_switch_it_commanded(tmp_path):
    """The start read-back must inspect the switch the start was sent to."""
    marker = tmp_path / "marker.json"
    c = _start_coord(marker, switch_state="on")
    c._resolve_emaldo_entity = _switch_resolver()
    c.hass.states.get = _switch_states(
        {
            "switch.commanded": "on",
            "switch.other": "off",
            # The prerequisite switch has to read a definite state, or the
            # start defers instead of starting.
            "switch.sell_back_to_grid": "on",
        }
    )

    asyncio.run(c._manage_manual_selling(_result(), _now(20, 0)))

    assert marker.exists(), (
        "the read-back must inspect the commanded switch, not a fresh "
        "resolution of it"
    )



def test_the_self_completion_reads_back_the_sale_own_switch(tmp_path):
    """The self-completion read must inspect the sale's own switch.

    The entity resolution is deliberately uncached, so resolving it again
    could land on a different switch - and then the read-back would report
    a state the sale does not have: the sale would look finished while it
    is still running, and the window it is selling would be stamped as
    served.
    """
    marker = tmp_path / "marker.json"
    c = _coord(marker, switch_state="on")
    c._manual_selling_active = True
    c._manual_selling_seen_on = True
    c._manual_selling_served = None
    c._manual_selling_started_at = _now(20, 0)
    c._manual_selling_start_window = (date(2026, 10, 20), WINDOW)
    # The sale was started on this switch...
    c._manual_selling_switch_id_cached = "switch.commanded"
    # ...but a fresh resolution lands on another one.
    c._resolve_emaldo_entity = MagicMock(
        side_effect=lambda key, domain="sensor": (
            "switch.other" if key == "manual_selling" else f"switch.{key}"
        )
    )
    c.hass.states.get = _switch_states(
        {"switch.commanded": "on", "switch.other": "off"}
    )

    asyncio.run(
        c._check_manual_selling_self_completion_locked(_result(), _now(20, 5))
    )

    assert c._manual_selling_active is True, (
        "the sale's own switch reads on - the sale is still running, not "
        "finished"
    )
    assert c._manual_selling_served is None, (
        "a running sale must not stamp the window as served"
    )
