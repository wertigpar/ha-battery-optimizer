"""Forced sell must fire on its own tick, not only after an optimizer run.

Cause A of issue #23: _manage_manual_selling() ran only at the tail of a full
optimizer run, so a window shorter than optimizer_interval opened and closed
between runs and never sold anything.

The follow-up commit under test serialises the actuator behind an
asyncio.Lock, gates the tick on forced sell being enabled, and rejects a plan
too old to trust.
"""
import asyncio
import inspect
import itertools
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import load_coordinator  # noqa: E402

# The real config key, so the tests that exercise the live options read cannot
# drift from the integration's own constant.
_FORCED_SELL_KEY = load_coordinator()[1].CONF_FORCED_SELL_ENABLED

INSIDE = datetime(2026, 10, 6, 7, 45)     # slot 31, inside window [30..32]
# Slot 33: just past the window, and close enough in time to INSIDE that the
# plan-age gate accepts it — the tick must be able to reach the actuator.
PAST_WINDOW = datetime(2026, 10, 6, 8, 15)


def _clock(moment):
    """patch dt_util.now to a fixed instant."""
    return patch.object(
        load_coordinator()[0].dt_util, "now", return_value=moment
    )


def _marker_path(case):
    """Sidecar path in a throwaway temp dir, per test.  Starts absent — that
    is the normal state for every install that never enabled the switch."""
    tmpdir = tempfile.mkdtemp(prefix="bo-marker-")
    case.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
    return os.path.join(tmpdir, "battery_optimizer_sell_back_to_grid.json")


def _write_marker(path, enabled=True):
    """Put a marker on disk directly — what a crashed session leaves behind."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"we_enabled": %s}' % ("true" if enabled else "false"))


def _inline_executor():
    """hass.async_add_executor_job, run on the spot — unit test, not HA."""
    async def job(func, *args):
        return func(*args)
    return job


def _hass_mock():
    """A fake hass whose async_add_executor_job runs the job on the spot.

    The coordinator hands its file helpers to the executor so the I/O stays
    off the event loop.  A bare MagicMock cannot be awaited, so a fixture
    that needs the file helpers has to provide one.  Fixtures that want to
    RECORD the executor jobs override the attribute afterwards.
    """
    hass = MagicMock()
    hass.async_add_executor_job = _inline_executor()
    return hass


_MARKER_DIR = tempfile.mkdtemp(prefix="bo-manual-sell-")
_MARKER_SEQ = itertools.count()


def _manual_sell_marker_path():
    """A marker path of this fixture's own, so one test's marker can
    never be read by another.  The directory is process-wide; the file name
    is unique per call.
    """
    return os.path.join(
        _MARKER_DIR,
        f"battery_optimizer_manual_sell_{next(_MARKER_SEQ)}.json",
    )


def _fake_entry(*, forced_sell=None):
    """`config` is a read-only property over _entry.data + _entry.options, so
    the fake entry — not a plain dict — is what has to be staged.  The age gate
    reads optimizer_interval through it via _config_int, and _forced_sell_enabled
    is a LIVE read of these options (an __init__ snapshot was the bug — an
    options-flow change never reached the tick), so the feature flag is staged
    here too.
    """
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    if forced_sell is not None:
        entry.options[_FORCED_SELL_KEY] = forced_sell
    return entry


def _tick_staged(c, *, last_run=INSIDE - timedelta(minutes=1),
                 last_attempt=None):
    """Stage the state the forced-sell tick reads but __init__ no longer
    supplies to a bare __new__ instance: the in-flight dedupe flag and the
    attempt anchor the age gate uses.
    """
    c._forced_sell_tick_in_flight = False
    c._last_run = last_run
    c._last_optimizer_attempt = (
        last_attempt if last_attempt is not None else last_run
    )
    return c


def _switch_state(state):
    """hass.states.get stub returning a real on/off state.

    An unreadable Sell back to grid state defers the forced-sell start (the
    sale cannot satisfy its own prerequisite), so a fixture that wants the
    start path must hand one over rather than leave a bare MagicMock.
    """
    obj = MagicMock()
    obj.state = state
    return MagicMock(return_value=obj)


def _manual_sell_state(c, *, active=False, served=None, seen_on=False,
                       started_at=None, attempts=0, start_window=None):
    """Stage the forced-sell actuator state a bare __new__ coordinator lacks.

    Plain assignment, never getattr with a default: a state variable that stops
    being initialised must fail loudly in the fixture that needs it, not be
    quietly invented as False at the point of use.
    """
    c._manual_selling_active = active
    c._manual_selling_served = served
    c._manual_selling_seen_on = seen_on
    c._manual_selling_started_at = started_at
    c._manual_selling_start_attempts = attempts
    c._manual_selling_start_window = start_window
    # True once this session owns the running sale (it started it, or it
    # adopted it after a restart).  Survives a stop attempt.
    c._manual_selling_ours = False
    # The forced-sell restart marker (issue #30).  A bare __new__ skips
    # __init__, so the path has to be staged here, or the marker helpers
    # raise AttributeError mid-sale.
    c._manual_sell_state_path = _manual_sell_marker_path()
    # The marker is already settled for this session, so the manage path's
    # reconciliation short-circuits — the same trick the fixtures use for
    # _sell_back_to_grid_restored.  The reconciliation itself is covered by
    # tests/test_forced_sell_restart.py.
    c._manual_sell_reconciled = True
    return c


class _LockSpy:
    """Wraps the real asyncio.Lock, exposing a deterministic "a caller is
    blocked on it" signal.

    asyncio.Lock offers no way to observe contention, so without this the race
    test would have to guess how many event-loop turns it takes the second
    caller to reach the acquire — i.e. it would pass by luck.
    """

    def __init__(self, real):
        self._real = real
        self.blocked = asyncio.Event()   # set when a caller found it locked
        self.entries = 0

    async def __aenter__(self):
        self.entries += 1
        if self._real.locked():
            self.blocked.set()
        await self._real.acquire()
        return self

    async def __aexit__(self, *exc):
        self._real.release()
        return False


class ForcedSellTickTests(unittest.TestCase):
    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, *, active=False, sell_slots=(30, 31, 32), target=5.0,
               emaldo_control=True, device_ok=True, forced_sell=True):
        """Build a coordinator with only the attributes the tick path reads."""
        c = self.C.__new__(self.C)
        _manual_sell_state(c, active=active)
        c._emaldo_control_enabled = emaldo_control
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=forced_sell)
        _tick_staged(c)
        # Issue #23 cause B state. Bare __new__ skips __init__, so these have
        # to be staged here or the real start path raises AttributeError.
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        # Post-restart restore already settled: these tests are about the
        # forced-sell evaluation, and the tick only schedules the restore
        # while this is false (its own tests stage it explicitly).
        c._sell_back_to_grid_restored = True

        result = MagicMock()
        result.sell_slots = list(sell_slots)
        result.sell_target_kwh = target
        c._last_result = result

        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=device_ok)
        c._stop_manual_selling = AsyncMock()
        c._stop_manual_selling_locked = AsyncMock()
        c._manage_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.services.async_call = AsyncMock()
        return c

    def test_tick_evaluates_while_window_is_open(self):
        """The whole point: a tick inside the window must act, with no
        preceding optimizer run."""
        c = self._coord()
        c._last_result.sell_slots = [30, 31, 32]   # 07:30-08:00
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_called_once()
        # Real check to replace the old tautology (`x is None` is never None):
        # the flag stays untouched until a start actually runs.
        self.assertFalse(c._manual_selling_active)

    def test_tick_is_noop_when_forced_sell_disabled(self):
        c = self._coord(emaldo_control=False)
        asyncio.run(c._evaluate_forced_sell())
        # Disabled control must not raise and must not start a window.
        c._manage_manual_selling.assert_not_called()

    def test_tick_is_noop_when_no_result(self):
        c = self._coord()
        c._last_result = None
        asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_not_called()

    def test_tick_stops_selling_outside_window(self):
        c = self._coord(active=True, sell_slots=(30, 31, 32))
        # Drop the fixture's stub so the *real* _manage_manual_selling runs:
        # the tick only delegates, so the stop decision — and therefore the
        # call this test asserts — lives in that method.  With the stub in
        # place the assertion could never hold and would prove nothing.
        del c._manage_manual_selling
        with _clock(PAST_WINDOW):
            asyncio.run(c._evaluate_forced_sell())
        # The body runs under the lock, so it must call the *_locked helper.
        # Re-entering _stop_manual_selling would deadlock — asyncio.Lock is
        # not reentrant.
        c._stop_manual_selling_locked.assert_awaited_once()
        c._stop_manual_selling.assert_not_awaited()


class ForcedSellTickCallbackTests(unittest.TestCase):
    """The timer must really schedule the work (issue #23 cause A).

    The tests above call _evaluate_forced_sell() directly, so they
    cannot tell a live tick from a dead one: with the callback body reduced
    to an unconditional return — issue #23 re-introduced — they all still
    pass. These pin the seam between timer and work: that the callback
    schedules, its guards, and the two arguments it hands down.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, *, emaldo_control=True, has_result=True,
               forced_sell=True):
        """Bare coordinator carrying only what the tick path reads:
        the config entry (forced sell read live from it), _emaldo_control_enabled,
        _last_result, hass — and a stubbed _manage_manual_selling so the
        assertions can see exactly what the tick hands it.
        """
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = emaldo_control
        c._last_result = MagicMock() if has_result else None
        c._entry = _fake_entry(forced_sell=forced_sell)
        _tick_staged(c)
        # Read by the forced-sell-disabled guard on both the callback and the
        # evaluate body, so a bare coordinator has to carry them.
        _manual_sell_state(c)
        c._sell_back_to_grid_saved = None
        # Restore already settled — see the note on the sibling fixture.
        c._sell_back_to_grid_restored = True
        c._manage_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.async_create_task = MagicMock(return_value=None)
        return c

    def _scheduled(self, c):
        """The single object the tick handed to the event loop."""
        c.hass.async_create_task.assert_called_once()
        return c.hass.async_create_task.call_args[0][0]

    def _restart_coord(self, *, restored=False):
        """Bare coordinator for the post-restart restore, driven by the tick.

        Deliberately the starved case: no plan in hand (_last_result is None,
        which is what a reboot gives you) and the marker already on disk with
        no in-memory flag.  That is the boot the startup watcher abandons when
        Emaldo's SoC never comes up, so run_optimizer never runs and the
        restore had no caller at all.
        """
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._last_result = None
        c._last_run = None
        c._entry = _fake_entry(forced_sell=True)
        c._forced_sell_tick_in_flight = False
        _manual_sell_state(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_restored = restored
        c._sell_back_to_grid_state_path = _marker_path(self)
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.hass = _hass_mock()
        c.hass.services.async_call = AsyncMock()
        c.hass.async_add_executor_job = _inline_executor()
        c.hass.async_create_task = MagicMock(return_value=None)
        _write_marker(c._sell_back_to_grid_state_path)
        return c

    def test_tick_drives_the_restart_restore_with_no_optimizer_run(self):
        """The starvation this closes: the switch used to stay ON for up to
        optimizer_interval (120 min by default) because run_optimizer was the
        only caller and the startup watcher never reached it.  The tick must
        drive it, above every other guard — with no plan and nothing else
        running."""
        c = self._restart_coord()
        c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro)

        c._forced_sell_tick_callback(INSIDE)

        # Exactly one task, and it is the restore (nothing else to evaluate).
        c.hass.async_create_task.assert_called_once()
        asyncio.run(c.hass.async_create_task.call_args[0][0])

        c.hass.services.async_call.assert_awaited_once()
        self.assertEqual(
            c.hass.services.async_call.call_args[0][:2], ("switch", "turn_off")
        )
        self.assertTrue(c._sell_back_to_grid_restored)
        self.assertFalse(os.path.exists(c._sell_back_to_grid_state_path))

    def test_tick_costs_nothing_once_the_restore_has_latched(self):
        """Installs that never touch this feature must not pay for it: once
        settled, the tick must not even allocate a coroutine."""
        c = self._restart_coord(restored=True)

        c._forced_sell_tick_callback(INSIDE)

        c.hass.async_create_task.assert_not_called()

    def test_tick_schedules_the_restore_without_holding_the_lock(self):
        """asyncio.Lock is not reentrant.  The tick is a sync @callback that
        hands the coroutine to the loop, and the coroutine takes the lock
        itself — so this completes instead of deadlocking (hard timeout)."""
        c = self._restart_coord()

        async def scenario():
            c.hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
            c._forced_sell_tick_callback(INSIDE)
            await asyncio.sleep(0)
            # Nothing else was queued, so this loop turn must settle the
            # restore rather than leave it queued behind a held lock.
            for _ in range(50):
                await asyncio.sleep(0)
                if c._sell_back_to_grid_restored:
                    return
            self.fail("restore never completed from the tick")

        asyncio.run(asyncio.wait_for(scenario(), timeout=5))
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_tick_schedules_the_manual_sell_recovery_while_the_latch_is_down(self):
        """A marker may still be on disk, so the tick must queue the
        forced-sell recovery.
        """
        c = self._coord(has_result=False)
        c._manual_sell_reconciled = False
        c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro)

        c._forced_sell_tick_callback(INSIDE)

        c.hass.async_create_task.assert_called_once()
        scheduled = c.hass.async_create_task.call_args[0][0]
        # A test that only inspects the coroutine must not leave an
        # un-awaited one behind.
        self.addCleanup(scheduled.close)
        self.assertIs(
            scheduled.cr_code, c._restore_manual_selling_after_restart.__code__
        )

    def test_tick_schedules_the_manual_sell_recovery_above_every_other_guard(self):
        """Forced sell disabled, with a plan in hand: the recovery is still
        queued.

        Every other guard would skip it - the disabled-feature guard, the
        in-flight guard, the no-plan guard - and the startup watcher may
        never reach run_optimizer after a reboot, which is exactly the state
        a reboot during a sell window leaves behind.
        """
        c = self._coord(forced_sell=False)
        c._manual_sell_reconciled = False
        c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro)

        c._forced_sell_tick_callback(INSIDE)

        c.hass.async_create_task.assert_called_once()
        scheduled = c.hass.async_create_task.call_args[0][0]
        self.addCleanup(scheduled.close)
        self.assertIs(
            scheduled.cr_code, c._restore_manual_selling_after_restart.__code__
        )

    def test_tick_schedules_nothing_once_the_reconcile_has_settled(self):
        """No marker left: the reconcile has settled, so the tick queues
        nothing at all — the same discipline the sell-back restore follows.
        """
        c = self._coord(has_result=False)
        c._manual_sell_reconciled = True

        c._forced_sell_tick_callback(INSIDE)

        c.hass.async_create_task.assert_not_called()

    def test_callback_schedules_the_evaluation(self):
        """A tick with control on and a result in hand must queue the work."""
        c = self._coord()
        c._forced_sell_tick_callback(None)

        scheduled = self._scheduled(c)
        # queued but never run here; closing it keeps a bare MagicMock's
        # bookkeeping from tripping "coroutine was never awaited".
        self.addCleanup(scheduled.close)
        self.assertTrue(inspect.iscoroutine(scheduled))
        # Same function, not a lookalike: code-object identity, resolved
        # against the real module's globals.  (That it is bound to *this*
        # instance is pinned by the two tests below, which run on `c`.)
        self.assertIs(scheduled.cr_code, c._evaluate_forced_sell.__code__)
        self.assertIs(scheduled.cr_frame.f_globals, vars(self.coord_mod))
        # Created, not started — the timer stays synchronous and hands the
        # coroutine over instead of blocking the event loop.
        self.assertEqual(inspect.getcoroutinestate(scheduled), "CORO_CREATED")

    def test_callback_inert_when_emaldo_control_disabled(self):
        """Control off means no forced sell, and no scheduled task either."""
        c = self._coord(emaldo_control=False)
        c._forced_sell_tick_callback(None)
        c.hass.async_create_task.assert_not_called()

    def test_callback_inert_without_a_result(self):
        """Nothing planned yet means nothing to evaluate."""
        c = self._coord(has_result=False)
        c._forced_sell_tick_callback(None)
        c.hass.async_create_task.assert_not_called()

    def test_evaluation_passes_the_current_clock(self):
        """The window decision needs now, so a stale clock misprices it."""
        c = self._coord()
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())

        c._manage_manual_selling.assert_awaited_once()
        args, kwargs = c._manage_manual_selling.await_args
        self.assertEqual(kwargs, {"start_allowed": True})
        self.assertIs(
            args[1], INSIDE, "must pass the live clock, not a cached one"
        )

    def test_evaluation_passes_the_latest_result(self):
        """Identity, not equality: the *current* plan, not a re-derived one."""
        c = self._coord()
        expected = c._last_result
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())

        args, _ = c._manage_manual_selling.await_args
        self.assertIs(args[0], expected)


class ForcedSellDisabledTests(unittest.TestCase):
    """FIX 4: forced sell defaults to False.  Installations without it must
    not allocate a coroutine and a task every 5 minutes for a feature they
    never turned on — even though _emaldo_control_enabled is True for most.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, *, active=False, saved=None):
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = True       # the leaky guard this replaces
        c._last_result = MagicMock()
        c._entry = _fake_entry(forced_sell=False)
        _tick_staged(c)
        # What the disabled path inspects: a sale in flight, and/or a Sell back
        # to grid enable we owe a restore for.  Both falsy by default, so the
        # default fixture is the "user never touches this feature" install.
        _manual_sell_state(c, active=active)
        c._sell_back_to_grid_saved = saved
        # Restore already settled — see the note on the sibling fixture.
        c._sell_back_to_grid_restored = True
        c._manage_manual_selling = AsyncMock()
        c._stop_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.async_create_task = MagicMock(return_value=None)
        return c

    def test_tick_schedules_nothing_when_feature_disabled(self):
        c = self._coord()
        self.assertTrue(c._emaldo_control_enabled)
        c._forced_sell_tick_callback(None)
        c.hass.async_create_task.assert_not_called()

    def test_evaluation_body_does_not_run_when_feature_disabled(self):
        c = self._coord()
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_not_awaited()

    # --- disabling mid-window must stop, not orphan ----------------------
    # A bare `return` on _forced_sell_enabled left the manual-selling switch ON
    # and the Sell back to grid enable un-restored, so the sale kept running
    # past its window with nothing managing it.

    def test_disabled_tick_still_tears_down_an_active_sale(self):
        c = self._coord(active=True)
        c._forced_sell_tick_callback(None)
        scheduled = c.hass.async_create_task.call_args[0][0]
        asyncio.run(scheduled)
        c._stop_manual_selling.assert_awaited_once()

    def test_disabled_tick_tears_down_when_only_the_switch_is_on(self):
        """A start that failed after the enable left nothing running, but the
        restore is still owed — keyed on our own flag, not on the sale."""
        c = self._coord(saved=True)
        c._forced_sell_tick_callback(None)
        scheduled = c.hass.async_create_task.call_args[0][0]
        asyncio.run(scheduled)
        c._stop_manual_selling.assert_awaited_once()

    def test_disabled_teardown_needs_no_usable_plan(self):
        """The teardown sits above the _last_result/_last_run/age gates on
        purpose: switching the feature off must stop the sale even when the plan
        is too old to act on or was never produced."""
        c = self._coord(active=True)
        c._last_result = None
        c._last_run = None
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._stop_manual_selling.assert_awaited_once()
        c._manage_manual_selling.assert_not_awaited()

    def test_disabled_with_nothing_active_stays_silent(self):
        """The cheap-property guarantee: no sale and nothing owed means no
        coroutine and no task allocated for a feature the user never turned on."""
        c = self._coord()
        c._forced_sell_tick_callback(None)
        c.hass.async_create_task.assert_not_called()

        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._stop_manual_selling.assert_not_awaited()


class ForcedSellPlanFreshnessTests(unittest.TestCase):
    """The tick's age gate: _last_result is only ever assigned, never cleared on
    a failed run, so an hours-old plan's sell_slots must not be reinterpreted
    against today's clock — that sells at yesterday's prices.

    Issue #23: the gate is anchored on _last_optimizer_attempt, not _last_run.
    _last_run only advances when a run produced a NEW plan, and
    _should_reoptimize returns False while the plan is being FOLLOWED — the
    normal steady state of an evening peak.  Anchored on _last_run, the gate
    closed 135 minutes into a healthy install and every later sell window was
    silently skipped.  The stop path must not be gated at all: a sale that has
    to end cannot be held open by a plan that is too old to start from.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.calls = []

    def _coord(self, last_run, last_attempt=..., *, active=False,
               real_actuator=False):
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = True
        result = MagicMock()
        result.sell_slots = [30, 31, 32]
        result.sell_target_kwh = 5.0
        c._last_result = result
        c._entry = _fake_entry(forced_sell=True)
        _tick_staged(
            c,
            last_run=last_run,
            last_attempt=None if last_attempt is ... else last_attempt,
        )
        _manual_sell_state(c, active=active)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_restored = True
        c._sell_back_to_grid_state_path = _marker_path(self)
        c._forced_sell_lock = asyncio.Lock()
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)
        c._manage_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.states.get = _switch_state("on")
        c.hass.async_add_executor_job = _inline_executor()
        c.hass.services.async_call = AsyncMock(
            side_effect=lambda domain, service, data=None, **k: (
                self.calls.append((domain, service,
                                   (data or {}).get("entity_id")))
            )
        )
        if real_actuator:
            # Drop the stub so the real _manage_manual_selling runs: the tick
            # only delegates, so the start/stop decision — and therefore the
            # service calls these tests assert — lives in that method.
            del c._manage_manual_selling
        return c

    def _switches(self):
        return [(svc, ent) for _d, svc, ent in self.calls if _d == "switch"]

    def test_fresh_plan_is_acted_on(self):
        # DEFAULT_OPTIMIZER_INTERVAL is 120 min; the gate allows interval + 15.
        c = self._coord(INSIDE - timedelta(minutes=120))
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_awaited_once()

    def test_stale_run_with_a_fresh_attempt_still_starts(self):
        """The regression this anchor fixes: the plan is hours old because every
        recent run was correctly skipped as "no significant changes", and the
        sell window is NOW.  Anchoring on _last_run missed it."""
        c = self._coord(INSIDE - timedelta(minutes=245),
                        last_attempt=INSIDE - timedelta(minutes=1))
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_awaited_once()
        _args, kwargs = c._manage_manual_selling.await_args
        self.assertTrue(kwargs["start_allowed"])

    def test_stale_attempt_and_run_do_not_start(self):
        """Anchor AND fallback old: the optimizer has not even tried for over an
        interval, so its view of the day is not current enough to sell on."""
        c = self._coord(INSIDE - timedelta(minutes=120 + 16))
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_not_awaited()

    def test_no_attempt_falls_back_to_the_restored_run(self):
        """After a restart a plan can be rehydrated with no attempt yet — the
        fallback is what keeps that plan sellable."""
        c = self._coord(INSIDE - timedelta(minutes=1), last_attempt=None)
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_awaited_once()

    def test_no_run_ever_is_ignored(self):
        c = self._coord(None)
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_not_awaited()

    def test_run_optimizer_stamps_the_attempt_before_every_guard(self):
        """The other half of the anchor: the stamp only helps if a run that
        SKIPS a rebuild still advances it.  So it has to be set before the SoC
        gate, before _should_reoptimize and before the no_change_skip return —
        a stamp below any of those is the original bug back."""
        source = inspect.getsource(self.C.run_optimizer)
        stamp = source.index("self._last_optimizer_attempt = dt_util.now()")
        for guard in ("soc = self._get_battery_soc()",
                      "self._should_reoptimize(soc, cfg)",
                      'state": "no_change_skip"'):
            self.assertLess(stamp, source.index(guard), guard)
        self.assertIn("self._last_optimizer_attempt: datetime | None = None",
                      inspect.getsource(self.C.__init__))

    # --- the stop path must survive the gate -----------------------------

    def test_stale_plan_still_stops_a_live_sale(self):
        """The gate closed the whole evaluation before, so a sale could run
        indefinitely past its window with a stale plan and nothing able to stop
        it.  The stop runs off the real actuator here, not a stub."""
        c = self._coord(INSIDE - timedelta(minutes=245), active=True,
                        real_actuator=True)
        with _clock(PAST_WINDOW):
            asyncio.run(c._evaluate_forced_sell())

        self.assertIn(
            ("turn_off", "switch.power_store_manual_selling"),
            self._switches(),
        )
        self.assertFalse(c._manual_selling_active)

    def test_stale_plan_refuses_to_open_a_new_sale(self):
        """Same stale plan, nothing running: the gate still holds, and the
        window check ran first so an ended window stops as usual."""
        c = self._coord(INSIDE - timedelta(minutes=245), real_actuator=True)
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())

        self.assertNotIn(
            ("turn_on", "switch.power_store_manual_selling"), self._switches()
        )
        self.assertFalse(c._manual_selling_active)

    def test_stale_plan_restores_a_switch_we_enabled(self):
        """The other half of the teardown: the Sell back to grid enable is
        withdrawn even when the plan is too old to start from."""
        c = self._coord(INSIDE - timedelta(minutes=245), real_actuator=True)
        c._sell_back_to_grid_saved = True
        with _clock(PAST_WINDOW):
            asyncio.run(c._evaluate_forced_sell())

        self.assertIn(
            ("turn_off", "switch.power_store_sell_back_to_grid"),
            self._switches(),
        )
        self.assertIsNone(c._sell_back_to_grid_saved)


class ForcedSellStartPathTests(unittest.TestCase):
    """FIX 5: every start-path test stubbed _manage_manual_selling, so nothing
    asserted that the switch is ever actually turned on.  Issue #23's symptom
    is zero export; these assert the real service calls.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.calls = []

    def _coord(self, *, active=False, target=5.0):
        c = self.C.__new__(self.C)
        _manual_sell_state(c, active=active)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=True)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)
        c.hass = _hass_mock()
        c.hass.states.get = _switch_state("on")
        c.hass.services.async_call = AsyncMock(
            side_effect=lambda *a, **k: self.calls.append((a[0], a[1]))
        )
        result = MagicMock()
        result.sell_slots = [30, 31, 32]
        result.sell_target_kwh = target
        self.result = result
        return c

    def test_start_actually_stages_target_and_turns_switch_on(self):
        """The real method, no stub: both service calls must be issued."""
        c = self._coord()
        asyncio.run(c._manage_manual_selling(self.result, INSIDE))

        self.assertIn(("number", "set_value"), self.calls)
        self.assertIn(("switch", "turn_on"), self.calls)
        self.assertTrue(c._manual_selling_active)
        # Staged before switched on: the number entity merges the intended
        # target while the switch is still off.
        self.assertLess(
            self.calls.index(("number", "set_value")),
            self.calls.index(("switch", "turn_on")),
        )

    def test_failed_start_leaves_flag_false(self):
        """A failed switch call must not leave a flag claiming to sell."""
        c = self._coord()
        c.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
        asyncio.run(c._manage_manual_selling(self.result, INSIDE))
        self.assertFalse(c._manual_selling_active)
        self.assertNotIn(("switch", "turn_on"), self.calls)

    def test_pv_only_window_stops_an_active_sale(self):
        """FIX 3: target <= 0 returned before the stop, so a plan turning
        PV-only mid-window left the switch on with nothing able to clear it.
        """
        c = self._coord(active=True, target=0.0)
        asyncio.run(c._manage_manual_selling(self.result, INSIDE))

        self.assertFalse(c._manual_selling_active)
        self.assertIn(("switch", "turn_off"), self.calls)
        self.assertNotIn(("switch", "turn_on"), self.calls)


class ForcedSellRaceTests(unittest.TestCase):
    """FIX 1 (CRITICAL): the run path and the tick both call
    _manage_manual_selling.  Unserialised, a stop interleaving between
    number.set_value and switch.turn_on leaves the switch ON while the flag is
    False — and every later _stop_manual_selling() early-returns on the flag,
    so nothing can ever turn it off again.

    The interleaving is forced, not timed: A parks inside number.set_value
    holding the lock, and the tick is only released once _LockSpy has
    observed it genuinely blocked on that lock.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, result, switch_calls, started, release, lock):
        c = self.C.__new__(self.C)
        _manual_sell_state(c)
        c._emaldo_control_enabled = True
        c._entry = _fake_entry(forced_sell=True)
        _tick_staged(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._last_result = result
        c._forced_sell_lock = lock
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)

        async def fake_call(domain, service, *a, **k):
            if domain == "switch":
                switch_calls.append(service)
            if domain == "number" and service == "set_value":
                started.set()
                await release.wait()      # suspend mid-start-sequence

        c.hass = _hass_mock()
        c.hass.states.get = _switch_state("on")
        c.hass.services.async_call = fake_call
        return c

    def test_stop_cannot_strand_the_switch_on(self):
        plan = MagicMock()
        plan.sell_slots = [30, 31, 32]
        plan.sell_target_kwh = 5.0

        started = asyncio.Event()
        release = asyncio.Event()
        switch_calls = []
        spy = _LockSpy(asyncio.Lock())
        c = self._coord(plan, switch_calls, started, release, spy)

        async def scenario():
            async def run_start():
                # Caller A: last slot of the window, starts selling.
                await c._manage_manual_selling(plan, INSIDE)

            async def run_tick():
                # Caller B: the 5-min tick, after the slot boundary passed.
                await started.wait()
                with _clock(PAST_WINDOW):
                    await c._evaluate_forced_sell()

            a = asyncio.create_task(run_start())
            b = asyncio.create_task(run_tick())
            await started.wait()
            # B has observed the lock held — i.e. it genuinely attempted its
            # stop while A was mid-sequence — so the interleaving is fixed.
            await asyncio.wait_for(spy.blocked.wait(), timeout=5)
            release.set()
            await asyncio.gather(a, b)

        asyncio.run(scenario())

        self.assertEqual(spy.entries, 2, "both callers must enter the lock")
        self.assertEqual(
            switch_calls, ["turn_on", "turn_off"],
            "the tick's stop must be serialised after the start, not lost",
        )
        self.assertEqual(
            switch_calls[-1:], ["turn_off"],
            "switch must end OFF, never stranded ON with the flag False",
        )
        self.assertFalse(c._manual_selling_active)


class SellBackToGridTests(unittest.TestCase):
    """Cause B of issue #23: Emaldo refuses Manual selling while
    power_store_sell_back_to_grid is off, so a correctly-planned window
    exported nothing (reporter: manual selling read back off for 3 minutes,
    battery flat at ~1.4 kW, no export).

    Every switch assertion pins the entity_id, never just ("switch",
    "turn_on"): the manual-selling start issues a turn_on of its own, so a
    domain/service-only check would pass with this whole feature absent.

    The fixture installs a real asyncio.Lock, so a reentrant acquire
    anywhere in the new path deadlocks and hangs rather than passing quietly.
    """

    SELL_BACK = "switch.power_store_sell_back_to_grid"
    MANUAL = "switch.power_store_manual_selling"

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.calls = []   # (domain, service, entity_id) in issue order
        self.marker = _marker_path(self)
        self.executor_jobs = []

    def _coord(self, *, sell_back_state, active=False, missing=False,
               sell_slots=(30, 31, 32)):
        c = self.C.__new__(self.C)
        _manual_sell_state(c, active=active)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=True)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_state_path = self.marker
        c._sell_back_to_grid_restored = False
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)

        def states_get(entity_id):
            if missing or self.SELL_BACK not in entity_id:
                return None
            obj = MagicMock()
            obj.state = sell_back_state
            return obj

        c.hass = _hass_mock()
        c.hass.states.get = MagicMock(side_effect=states_get)
        c.hass.services.async_call = self._recorder()
        c.hass.async_add_executor_job = self._executor()
        c.hass.async_create_task = MagicMock(return_value=None)
        return c

    def _executor(self):
        """Runs the executor function inline, but records that it went through
        hass.async_add_executor_job — so a test can prove the file I/O is off
        the event loop rather than blocking it."""
        async def job(func, *args):
            self.executor_jobs.append(func.__name__)
            return func(*args)
        return job

    def _recorder(self):
        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
        return call

    def _result(self, target=5.0, sell_slots=(30, 31, 32)):
        r = MagicMock()
        r.sell_slots = list(sell_slots)
        r.sell_target_kwh = target
        return r

    def _on(self, entity_id):
        """(service, entity_id) for every call aimed at one switch."""
        return [(s, e) for _d, s, e in self.calls if e == entity_id]

    # --- window opens ---------------------------------------------------

    def test_window_open_enables_sell_back_to_grid_first(self):
        """The prerequisite has to be in place BEFORE the sale is requested —
        asking Emaldo to sell first is the bug, not the fix."""
        c = self._coord(sell_back_state="off")
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertIn(("turn_on", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertLess(
            self.calls.index(("switch", "turn_on", self.SELL_BACK)),
            self.calls.index(("switch", "turn_on", self.MANUAL)),
        )
        self.assertTrue(c._manual_selling_active)
        # True == we flipped it, so a restore is owed.
        self.assertIs(c._sell_back_to_grid_saved, True)

    def test_window_open_leaves_a_switch_the_user_owns_alone(self):
        """Already on => remember nothing, so the stop path cannot take away a
        setting we did not make."""
        c = self._coord(sell_back_state="on")
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertEqual(self._on(self.SELL_BACK), [])
        self.assertIsNone(c._sell_back_to_grid_saved)
        # The sale itself still proceeds.
        self.assertIn(("turn_on", self.MANUAL), self._on(self.MANUAL))

    def test_unreadable_state_is_never_written(self):
        """Missing / unknown / unavailable: no write, and nothing remembered,
        so the stop path leaves whatever is out there alone — and no sale is
        started either, because the sale cannot satisfy its own prerequisite."""
        for label, state, missing in (
            ("missing", "off", True),
            ("unknown", "unknown", False),
            ("unavailable", "unavailable", False),
        ):
            with self.subTest(case=label):
                self.calls.clear()
                c = self._coord(sell_back_state=state, missing=missing)
                asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

                self.assertEqual(self._on(self.SELL_BACK), [])
                self.assertIsNone(c._sell_back_to_grid_saved)
                # ... and the start is deferred, not forced through.
                self.assertEqual(self._on(self.MANUAL), [])
                self.assertFalse(c._manual_selling_active)

    def test_failed_enable_still_starts_the_sale(self):
        """A prerequisite that will not flip must not silently skip the sale:
        the window is time-boxed, so a lost sale is recoverable."""
        c = self._coord(sell_back_state="off")

        async def call(domain, service, data=None, **kwargs):
            entity = (data or {}).get("entity_id")
            self.calls.append((domain, service, entity))
            if entity == self.SELL_BACK:
                raise RuntimeError("switch unavailable")

        c.hass.services.async_call = call
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertIn(("turn_on", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIn(("turn_on", self.MANUAL), self._on(self.MANUAL))
        self.assertTrue(c._manual_selling_active)
        # Nothing was flipped, so nothing is owed.
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_an_unknown_state_defers_the_start_to_the_next_tick(self):
        """The sale cannot satisfy its own prerequisite without the switch, and
        _manual_selling_active would short-circuit every later tick — so the
        enable has to be retried rather than latched behind a DEBUG line."""
        c = self._coord(sell_back_state="unknown")

        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertEqual(self.calls, [])
        self.assertFalse(c._manual_selling_active)
        self.assertIsNone(c._sell_back_to_grid_saved)

        # The next tick, with the switch readable, starts the window.
        self.calls.clear()
        c2 = self._coord(sell_back_state="on")
        asyncio.run(c2._manage_manual_selling(self._result(), INSIDE))
        self.assertIn(("turn_on", self.MANUAL), self._on(self.MANUAL))

    # --- a device that stopped resolving must not strand the teardown -----

    def test_an_unresolvable_device_still_stops_a_live_sale(self):
        """resolve_emaldo_device() used to return above every stop branch, so
        losing the binding mid-window stranded the manual-selling switch."""
        c = self._coord(sell_back_state="on", active=True)
        c.resolve_emaldo_device = MagicMock(return_value=False)

        asyncio.run(c._manage_manual_selling(self._result(), PAST_WINDOW))

        self.assertIn(("turn_off", self.MANUAL), self._on(self.MANUAL))
        self.assertFalse(c._manual_selling_active)

    def test_an_unresolvable_device_still_restores_the_switch(self):
        """... and the Sell back to grid enable with it."""
        c = self._coord(sell_back_state="on")
        c.resolve_emaldo_device = MagicMock(return_value=False)
        c._sell_back_to_grid_saved = True

        asyncio.run(c._manage_manual_selling(self._result(), PAST_WINDOW))

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_an_unresolvable_device_still_blocks_a_new_sale(self):
        """Fail-safe kept: no device means no selling, device just idles."""
        c = self._coord(sell_back_state="on")
        c.resolve_emaldo_device = MagicMock(return_value=False)

        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertEqual(self._on(self.MANUAL), [])
        self.assertFalse(c._manual_selling_active)

    # --- window closes --------------------------------------------------

    def test_stop_restores_a_switch_we_enabled(self):
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        asyncio.run(c._stop_manual_selling())

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        # Cleared, so a later stop cannot turn it off a second time.
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_stop_leaves_a_switch_we_never_touched(self):
        """saved None (untouched) and saved False (already on) are both the
        user's switch: neither may be turned off."""
        for saved in (None, False):
            with self.subTest(saved=saved):
                self.calls.clear()
                c = self._coord(sell_back_state="on", active=True)
                c._sell_back_to_grid_saved = saved
                asyncio.run(c._stop_manual_selling())

                self.assertEqual(self._on(self.SELL_BACK), [])
                # The manual-selling switch is still stopped.
                self.assertIn(("turn_off", self.MANUAL), self._on(self.MANUAL))

    def test_stop_restores_even_when_the_sale_never_started(self):
        """A failed start leaves the flag False with the prerequisite already
        flipped. The restore is keyed on our own flag, not on the sale
        running, so this cannot strand the user's switch on forever."""
        c = self._coord(sell_back_state="on", active=False)
        c._sell_back_to_grid_saved = True
        asyncio.run(c._stop_manual_selling())

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_stop_restores_after_the_manual_selling_switch(self):
        """Order: the sale stops before its prerequisite is withdrawn."""
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        asyncio.run(c._stop_manual_selling())

        self.assertLess(
            self.calls.index(("switch", "turn_off", self.MANUAL)),
            self.calls.index(("switch", "turn_off", self.SELL_BACK)),
        )

    # --- a failed restore must not be abandoned ---------------------------
    # _sell_back_to_grid_saved is the only record that a restore is owed. The
    # unconditional clear after the except dropped that record on failure: the
    # switch stayed ON and the next 5-min tick's restore returned on
    # `if not self._sell_back_to_grid_saved`, so nothing ever retried.

    def test_successful_restore_clears_the_flag(self):
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        asyncio.run(c._restore_sell_back_to_grid())

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIsNone(c._sell_back_to_grid_saved)
        # Nothing owed any more, so no later call re-issues the write.
        self.calls.clear()
        asyncio.run(c._restore_sell_back_to_grid())
        self.assertEqual(self._on(self.SELL_BACK), [])

    def test_failed_restore_keeps_the_flag_and_the_next_tick_retries(self):
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        attempts = []

        async def flaky(domain, service, data=None, **kwargs):
            entity = (data or {}).get("entity_id")
            self.calls.append((domain, service, entity))
            if entity == self.SELL_BACK and service == "turn_off":
                attempts.append(entity)
                raise RuntimeError("switch unavailable")

        c.hass.services.async_call = flaky
        asyncio.run(c._stop_manual_selling())

        self.assertEqual(len(attempts), 1)
        # Still owed: clearing it here is what stranded the switch ON.
        self.assertIs(c._sell_back_to_grid_saved, True)

        # The next tick's stop must reach the service call again.
        asyncio.run(c._restore_sell_back_to_grid())
        self.assertEqual(len(attempts), 2)
        self.assertIs(c._sell_back_to_grid_saved, True)

        # Once the service call works, the retry clears the record.
        c.hass.services.async_call = self._recorder()
        asyncio.run(c._restore_sell_back_to_grid())
        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_a_hung_restore_times_out_and_keeps_the_flag(self):
        """_forced_sell_lock is held across the call, so an unbounded one stalls
        the optimizer run and every tick queued behind it.  Bounded, like the
        restart path; on timeout the restore is still owed, so neither the flag
        nor the marker may be dropped."""
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()
        never = asyncio.Event()

        async def hung(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
            await never.wait()          # never set: hangs forever

        c.hass.services.async_call = hung

        async def scenario():
            with patch.object(self.coord_mod,
                              "_SELL_BACK_RESTORE_TIMEOUT_S", 0.05):
                await asyncio.wait_for(c._restore_sell_back_to_grid(),
                                       timeout=5)

        with self.assertLogs(self.coord_mod._LOGGER, level="ERROR") as cap:
            asyncio.run(scenario())

        self.assertTrue(any("Timed out" in r for r in cap.output))
        self.assertIs(c._sell_back_to_grid_saved, True)
        self.assertTrue(os.path.exists(self.marker))
        self.assertNotIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    def test_failed_restore_log_says_it_is_still_owed(self):
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True

        async def broken(domain, service, data=None, **kwargs):
            raise RuntimeError("switch unavailable")

        c.hass.services.async_call = broken
        # _LOGGER = logging.getLogger(__name__) inside the loaded module.
        with self.assertLogs(self.coord_mod.__name__, level="ERROR") as logs:
            asyncio.run(c._restore_sell_back_to_grid())

        message = "\n".join(logs.output)
        self.assertIn(self.SELL_BACK, message)
        self.assertIn("retry", message.lower())

    # --- guards and reentrancy ------------------------------------------

    def test_nothing_runs_when_forced_sell_is_disabled(self):
        """_plan_forced_sell_slots() returns empty when the feature is off, so
        a disabled install reaches the stop branch — which must not turn the
        switch on for a window that does not exist."""
        c = self._coord(sell_back_state="off")
        asyncio.run(c._manage_manual_selling(self._result(target=0.0, sell_slots=()),
                                            INSIDE))

        self.assertEqual(self._on(self.SELL_BACK), [])
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_closed_window_never_enables_the_switch(self):
        """Past the window the stop branch runs; the enable must not."""
        c = self._coord(sell_back_state="off")
        asyncio.run(c._manage_manual_selling(self._result(), PAST_WINDOW))

        self.assertEqual(self._on(self.SELL_BACK), [])
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_stop_reached_from_inside_the_lock_completes(self):
        """The restore also runs on a stop reached from
        _manage_manual_selling_locked, i.e. already holding the lock. If any
        leg of that path re-entered the public _stop_manual_selling, this
        would block forever on the non-reentrant lock rather than return —
        so it is run under a hard timeout."""
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        # target 0 -> PV-only -> stop from inside the locked body.
        asyncio.run(asyncio.wait_for(
            c._manage_manual_selling(self._result(target=0.0), INSIDE), timeout=5))

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))

    def test_concurrent_start_and_stop_both_complete(self):
        """Same lock, both public entry points: the tick stopping a start in
        flight must serialise, not deadlock."""
        c = self._coord(sell_back_state="off")

        async def scenario():
            start = asyncio.create_task(c._manage_manual_selling(self._result(),
                                                                 INSIDE))
            stop = asyncio.create_task(c._stop_manual_selling())
            await asyncio.wait_for(asyncio.gather(start, stop), timeout=5)

        asyncio.run(scenario())
        self.assertFalse(c._manual_selling_active)

    def test_switch_id_falls_back_when_resolution_fails(self):
        """Auto-discovery can miss (the Emaldo entity registry can still be
        empty on the first tick after boot), so the documented fallback is
        used — and is not cached, so a later successful resolution wins."""
        c = self._coord(sell_back_state="on")
        c._resolve_emaldo_entity = MagicMock(return_value=None)

        self.assertEqual(c._sell_back_to_grid_switch_id(), self.SELL_BACK)
        self.assertIsNone(c._sell_back_to_grid_switch_id_cached)

        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.emaldo_{key}")
        self.assertEqual(c._sell_back_to_grid_switch_id(),
                         "switch.emaldo_sell_back_to_grid")


class SellBackToGridRestartTests(unittest.TestCase):
    """_sell_back_to_grid_saved is in-memory, so an unclean restart loses the
    only record that we enabled the switch and strands it ON for the rest of
    the day with nothing managing it.  async_shutdown cannot await, so the
    restore has to survive on disk instead.

    Deliberately NOT runtime_state.json: rebuild_runtime() rejects a payload
    whose last_run is not today and returns None on empty plan_slots, while
    this marker has to outlive the day and is not a plan.
    """

    SELL_BACK = "switch.power_store_sell_back_to_grid"

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.calls = []
        self.marker = _marker_path(self)
        self.executor_jobs = []

    def _coord(self, *, sell_back_state="on", active=False, marker=None):
        c = self.C.__new__(self.C)
        _manual_sell_state(c, active=active)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=True)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_state_path = self.marker
        c._sell_back_to_grid_restored = False
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)

        def states_get(entity_id):
            if self.SELL_BACK not in entity_id:
                return None
            obj = MagicMock()
            obj.state = sell_back_state
            return obj

        c.hass = _hass_mock()
        c.hass.states.get = MagicMock(side_effect=states_get)
        c.hass.services.async_call = self._recorder()
        c.hass.async_add_executor_job = self._executor()
        c.hass.async_create_task = MagicMock(return_value=None)
        if marker:
            # The state a crashed session leaves behind: the marker on disk,
            # and — this is the point — NO in-memory flag, because the process
            # that set it is gone.
            c._sell_back_to_grid_saved = True
            c._persist_sell_back_to_grid_state()
            c._sell_back_to_grid_saved = None
        return c

    def _executor(self):
        """Inline execution, but every hop through
        hass.async_add_executor_job is recorded by name — so a test can prove
        the file I/O never runs on the event loop."""
        async def job(func, *args):
            self.executor_jobs.append(func.__name__)
            return func(*args)
        return job

    def _recorder(self):
        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
        return call

    def _result(self, target=5.0, sell_slots=(30, 31, 32)):
        r = MagicMock()
        r.sell_slots = list(sell_slots)
        r.sell_target_kwh = target
        return r

    def _on(self, entity_id=None):
        entity_id = entity_id or self.SELL_BACK
        return [(s, e) for _d, s, e in self.calls if e == entity_id]

    def _marker_present(self):
        return os.path.exists(self.marker)

    # --- the marker is written and removed ------------------------------

    def test_persist_writes_the_marker_and_clear_removes_it(self):
        c = self._coord()
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()

        self.assertTrue(c._load_sell_back_to_grid_state())
        c._clear_sell_back_to_grid_state()
        self.assertIsNone(c._load_sell_back_to_grid_state())

    def test_persist_does_nothing_when_we_did_not_touch_the_switch(self):
        """saved None (never touched) and saved False (the user's own switch)
        both mean no restore is owed, so no marker may be written — one would
        make the next startup switch off a setting we never made."""
        for saved in (None, False):
            with self.subTest(saved=saved):
                c = self._coord()
                c._sell_back_to_grid_saved = saved
                c._persist_sell_back_to_grid_state()

                self.assertFalse(self._marker_present())
                self.assertIsNone(c._load_sell_back_to_grid_state())

    def test_marker_load_separates_absent_from_unreadable(self):
        """The two must never collapse into one value: "absent" latches the
        one-shot restore guard, "unreadable" must not — a truncated marker is
        precisely the state a power cut leaves, and reading it as "nothing to
        restore" switches the safety net off for the whole session.
        """
        unreadable = self.coord_mod._SELL_BACK_MARKER_UNREADABLE
        for label, body in (
            ("truncated", '{"we_enabled": tr'),
            ("not json", "{ this is not json"),
            ("not an object", '["we_enabled"]'),
            ("wrong value type", '{"we_enabled": "yes"}'),
        ):
            with self.subTest(case=label):
                c = self._coord()
                if self._marker_present():
                    os.remove(self.marker)   # a prior subTest left one behind
                self.assertIsNone(c._load_sell_back_to_grid_state())
                with open(self.marker, "w", encoding="utf-8") as fh:
                    fh.write(body)
                self.assertIs(c._load_sell_back_to_grid_state(), unreadable)

    def test_clear_is_a_noop_when_no_marker_exists(self):
        c = self._coord()
        c._clear_sell_back_to_grid_state()   # must not raise

    # --- the enable path writes it --------------------------------------

    def test_window_open_persists_the_marker(self):
        """The gap this whole task closes: the enable must reach disk, or a
        power loss mid-window leaves no trace at all."""
        c = self._coord(sell_back_state="off")
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertIn(("turn_on", self.SELL_BACK), self._on())
        self.assertTrue(c._load_sell_back_to_grid_state())
        # Off the event loop, never inline.
        self.assertIn("_persist_sell_back_to_grid_state", self.executor_jobs)

    def test_no_marker_for_a_switch_the_user_owns(self):
        c = self._coord(sell_back_state="on")
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertFalse(self._marker_present())

    def test_failed_enable_records_no_flag_and_leaves_no_marker(self):
        """Nothing was flipped, so neither the flag nor the marker may claim a
        restore is owed: the marker exists to record OUR flip, and a marker left
        behind would outlive the window and make the next startup turn off a
        switch we never turned on — taking away a setting the user owns."""
        c = self._coord(sell_back_state="off")

        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
            if (data or {}).get("entity_id") == self.SELL_BACK:
                raise RuntimeError("switch unavailable")

        c.hass.services.async_call = call
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertIsNone(c._sell_back_to_grid_saved)
        self.assertFalse(self._marker_present())
        self.assertIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    def test_a_failed_enable_leaves_no_marker_for_the_next_boot(self):
        """The end of the chain the marker exists for: with it cleared, the next
        startup finds nothing to restore and never touches the switch.  Left in
        place it would turn off a switch this component never turned on —
        breaking the promise that a user's own setting is left alone."""
        c = self._coord(sell_back_state="off")

        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
            if (data or {}).get("entity_id") == self.SELL_BACK:
                raise RuntimeError("switch unavailable")

        c.hass.services.async_call = call
        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        # A fresh coordinator stands in for the reboot: the in-memory flags are
        # gone, so only the marker file could speak.
        after_restart = self._coord()
        self.assertFalse(after_restart._sell_back_to_grid_restored)
        asyncio.run(after_restart._restore_sell_back_to_grid_after_restart())

        self.assertNotIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertTrue(after_restart._sell_back_to_grid_restored)

    def test_the_tick_never_re_persists_the_marker(self):
        """A read-only config dir would otherwise retry a doomed fsync once per
        5-minute tick for the life of the install.  Documented choice: persist
        at window open only, and say so in the code comment."""
        c = self._coord(sell_back_state="on", active=True)
        c._sell_back_to_grid_saved = True
        c._clear_sell_back_to_grid_state()

        for _ in range(3):
            asyncio.run(c._restore_sell_back_to_grid())

        self.assertNotIn("_persist_sell_back_to_grid_state", self.executor_jobs)
        self.assertFalse(self._marker_present())

    # --- a successful restore removes it --------------------------------

    def test_stop_clears_the_marker_once_the_restore_succeeds(self):
        c = self._coord(active=True)
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()

        asyncio.run(c._stop_manual_selling())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertIsNone(c._sell_back_to_grid_saved)
        self.assertFalse(self._marker_present())
        self.assertIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    def test_stop_clears_the_marker_from_inside_the_lock(self):
        """The stop that a plan change reaches runs from
        _manage_manual_selling_locked, i.e. already holding the lock.  If the
        marker were cleared anywhere else, this path would strand it — run
        under a hard timeout so a reentrant lock acquire fails instead of
        hanging the suite."""
        c = self._coord(active=True)
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()

        asyncio.run(asyncio.wait_for(
            c._manage_manual_selling(self._result(target=0.0), INSIDE), timeout=5))

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertFalse(self._marker_present())

    def test_failed_restore_keeps_the_marker_for_the_next_startup(self):
        """Clearing on failure is what strands the switch ON: the in-memory
        flag keeps the tick retrying, but nothing on disk would tell a fresh
        process that a restore is owed."""
        c = self._coord(active=True)
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()

        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
            raise RuntimeError("switch unavailable")

        c.hass.services.async_call = call
        asyncio.run(c._stop_manual_selling())

        self.assertIs(c._sell_back_to_grid_saved, True)
        self.assertTrue(c._load_sell_back_to_grid_state())
        self.assertNotIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    # --- the startup restore --------------------------------------------

    def test_restart_turns_the_switch_off_and_clears_the_marker(self):
        c = self._coord(marker=True)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertFalse(self._marker_present())
        # Marked done, so a later run_optimizer does not re-issue the write.
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_restart_does_nothing_without_a_marker(self):
        """The persistence has to be load-bearing: delete the file and the
        restore must be skipped entirely, not re-derived from anything."""
        c = self._coord(marker=True)
        os.remove(self.marker)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertEqual(self._on(), [])
        # Guard still latched: nothing was owed, nothing to retry.
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_restart_runs_once_per_session(self):
        c = self._coord(marker=True)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())
        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertEqual(len(self._on()), 1)

    def test_failed_restart_keeps_the_marker_and_retries_next_run(self):
        c = self._coord(marker=True)
        attempts = []

        async def call(domain, service, data=None, **kwargs):
            entity = (data or {}).get("entity_id")
            self.calls.append((domain, service, entity))
            attempts.append(entity)
            raise RuntimeError("switch unavailable")

        c.hass.services.async_call = call
        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertEqual(len(attempts), 1)
        self.assertTrue(self._marker_present())
        # Guard NOT latched — the next run_optimizer has to try again.
        self.assertFalse(c._sell_back_to_grid_restored)

        c.hass.services.async_call = self._recorder()
        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertFalse(self._marker_present())
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_restart_works_without_the_in_memory_flag(self):
        """This is the whole point: a fresh process has no
        _sell_back_to_grid_saved, so the restore must be driven by the file
        alone rather than short-circuiting on the flag."""
        c = self._coord(marker=True)
        self.assertIsNone(c._sell_back_to_grid_saved)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())

    def test_restart_runs_all_file_io_on_the_executor(self):
        c = self._coord(marker=True)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn("_load_sell_back_to_grid_state", self.executor_jobs)
        self.assertIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    def test_restart_completes_while_a_forced_sell_start_holds_the_lock(self):
        """The restore takes the same lock as the actuator, so a tick
        enabling the switch mid-restore cannot be undone by it — and, more
        importantly here, the restore must not deadlock behind it.  Timeout so
        a non-reentrant re-acquire fails the test instead of hanging."""
        # sell_back_state off: the start must actually flip the switch, so the
        # two tasks contend for the lock on real work rather than a no-op.
        c = self._coord(sell_back_state="off", marker=True)

        async def scenario():
            holder = asyncio.create_task(
                c._manage_manual_selling(self._result(), INSIDE))
            await asyncio.sleep(0)   # let the start take the lock
            restore = asyncio.create_task(
                c._restore_sell_back_to_grid_after_restart())
            await asyncio.wait_for(
                asyncio.gather(holder, restore), timeout=5)

        asyncio.run(scenario())

        # The start enabled it (marker rewritten), then the restore put it off
        # and dropped the marker again.
        self.assertIn(("turn_on", self.SELL_BACK), self._on())
        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertLess(
            self.calls.index(("switch", "turn_on", self.SELL_BACK)),
            self.calls.index(("switch", "turn_off", self.SELL_BACK)),
        )
        self.assertFalse(self._marker_present())

    def test_restart_is_wired_into_run_optimizer(self):
        """The seam itself: nothing else calls it, so without this call the
        restore could exist and still never run."""
        source = inspect.getsource(self.C.run_optimizer)
        self.assertIn("_restore_sell_back_to_grid_after_restart()", source)

    def test_setup_call_site_precedes_the_optimizer_work(self):
        """Placed next to the runtime-state restore, before the SoC guards:
        a marker must be honoured even on a boot where the optimizer later
        decides not to run at all."""
        source = inspect.getsource(self.C.run_optimizer)
        self.assertLess(
            source.index("_restore_sell_back_to_grid_after_restart()"),
            source.index("_LOGGER.info(\"Optimizer triggered"),
        )
        # ... and before the SoC gate, so a boot where the optimizer declines
        # to run still honours the marker.
        self.assertLess(
            source.index("_restore_sell_back_to_grid_after_restart()"),
            source.index("soc = self._get_battery_soc()"),
        )

    # --- FIX 2: the marker must be atomic, and unreadable is not absent ----

    def _stray_files(self):
        """Everything in the marker dir except the marker itself — a partial
        write would leave its file behind."""
        return [n for n in os.listdir(os.path.dirname(self.marker))
                if n != os.path.basename(self.marker)]

    def test_valid_marker_latches_only_after_a_successful_restore(self):
        c = self._coord(marker=True)

        asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_corrupt_marker_does_not_latch_and_is_retried(self):
        """The failure this commit exists to survive: the old write truncated
        in place, so a power cut during json.dump left `{"we_enabled": tr`,
        which read back as corrupt — and a corrupt read was latched as "no
        marker", disabling the recovery for the rest of the session, silently.
        """
        for label, body in (
            ("truncated", '{"we_enabled": tr'),
            ("not json", "{ this is not json"),
            ("not an object", '["we_enabled"]'),
            ("wrong value type", '{"we_enabled": "yes"}'),
        ):
            with self.subTest(case=label):
                c = self._coord()
                self.calls.clear()          # a prior subTest's retry ran here
                self.executor_jobs.clear()
                with open(self.marker, "w", encoding="utf-8") as fh:
                    fh.write(body)

                with self.assertLogs(self.coord_mod._LOGGER, level="ERROR"):
                    asyncio.run(c._restore_sell_back_to_grid_after_restart())

                self.assertFalse(c._sell_back_to_grid_restored)
                # We do not know what the file meant, so we must not touch a
                # switch on the strength of it.
                self.assertEqual(self._on(), [])
                self.assertEqual(self.executor_jobs,
                                 ["_load_sell_back_to_grid_state"])
                self.assertTrue(self._marker_present())

                # ... and the next tick gets a real go once the file reads.
                c._sell_back_to_grid_saved = True
                c._persist_sell_back_to_grid_state()
                asyncio.run(c._restore_sell_back_to_grid_after_restart())
                self.assertIn(("turn_off", self.SELL_BACK), self._on())
                self.assertTrue(c._sell_back_to_grid_restored)

    def test_persist_leaves_no_temp_file_behind_when_the_rename_fails(self):
        c = self._coord()
        c._sell_back_to_grid_saved = True

        with patch.object(self.coord_mod.os, "replace",
                          side_effect=OSError("disk full")):
            c._persist_sell_back_to_grid_state()   # must not raise

        self.assertEqual(self._stray_files(), [])
        self.assertFalse(self._marker_present())

    def test_a_failed_write_cannot_truncate_an_existing_marker(self):
        """The exact bug: open(path, "w") truncates in place, so a failure
        partway through left a partial file where a good one had been."""
        c = self._coord()
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state()
        with open(self.marker, encoding="utf-8") as fh:
            good = fh.read()

        def half_written(_payload, fh, *a, **kw):
            fh.write('{"we_enabled": tr')   # what a power cut leaves
            fh.flush()
            raise OSError("power cut")

        with patch.object(self.coord_mod.json, "dump", side_effect=half_written):
            c._persist_sell_back_to_grid_state()

        with open(self.marker, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), good)
        self.assertIs(c._load_sell_back_to_grid_state(), True)
        self.assertEqual(self._stray_files(), [])

    # --- FIX 3: the restore must not be able to hang the loop -------------

    def test_hung_service_call_times_out_without_latching(self):
        """_forced_sell_lock is held across the call, so an unbounded one
        stalls the first optimizer run and every tick queued behind it."""
        c = self._coord(marker=True)
        never = asyncio.Event()

        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
            await never.wait()          # never set: hangs forever

        c.hass.services.async_call = call

        async def scenario():
            with patch.object(self.coord_mod,
                              "_SELL_BACK_RESTORE_TIMEOUT_S", 0.05):
                await asyncio.wait_for(
                    c._restore_sell_back_to_grid_after_restart(), timeout=5)

        with self.assertLogs(self.coord_mod._LOGGER, level="ERROR") as cap:
            asyncio.run(scenario())

        self.assertTrue(any("Timed out" in r for r in cap.output))
        self.assertFalse(c._sell_back_to_grid_restored)
        self.assertTrue(self._marker_present())   # a restore is still owed
        self.assertNotIn("_clear_sell_back_to_grid_state", self.executor_jobs)

    # --- FIX 4: never cut the prerequisite out from under a live sale -----

    def test_live_window_blocks_the_restart_restore(self):
        """Turning Sell back to grid off mid-window withdraws the very
        prerequisite the running sale depends on, so Emaldo would refuse it —
        with manual_selling still on.  Skip, and leave the marker for after."""
        c = self._coord(marker=True)
        c._last_result = self._result(sell_slots=(30, 31, 32))  # INSIDE=slot 31

        with _clock(INSIDE):
            asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertEqual(self._on(), [])
        self.assertFalse(c._sell_back_to_grid_restored)
        self.assertTrue(self._marker_present())

        # ... and once the window has closed the marker is honoured.
        with _clock(PAST_WINDOW):
            asyncio.run(c._restore_sell_back_to_grid_after_restart())
        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_no_plan_permits_the_restart_restore(self):
        """Post-reboot _last_result is None.  That is the stranded-switch case
        this feature exists for, so the missing plan must not block it."""
        c = self._coord(marker=True)
        c._last_result = None

        with _clock(INSIDE):
            asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())
        self.assertTrue(c._sell_back_to_grid_restored)

    def test_a_past_window_does_not_block_the_restart_restore(self):
        c = self._coord(marker=True)
        c._last_result = self._result(sell_slots=(30, 31, 32))

        with _clock(PAST_WINDOW):   # slot 33, just past the window
            asyncio.run(c._restore_sell_back_to_grid_after_restart())

        self.assertIn(("turn_off", self.SELL_BACK), self._on())

    # --- FIX 5: record the enable before making it, loudly on failure -----

    def test_marker_is_written_before_the_enable(self):
        """Ordering is the crash-safety: a marker with no enable behind it
        costs one redundant turn_off, an enable with no marker behind it is the
        stranded switch."""
        c = self._coord(sell_back_state="off")
        seq = []

        async def job(func, *args):
            seq.append(func.__name__)
            return func(*args)

        async def call(domain, service, data=None, **kwargs):
            seq.append(service)
            self.calls.append((domain, service, (data or {}).get("entity_id")))

        c.hass.async_add_executor_job = job
        c.hass.services.async_call = call

        asyncio.run(c._manage_manual_selling(self._result(), INSIDE))

        self.assertLess(
            seq.index("_persist_sell_back_to_grid_state"), seq.index("turn_on")
        )

    def test_persist_failure_is_logged_as_an_error(self):
        """A swallowed persist failure is the whole risk: the session looks
        healthy (the in-memory flag still drives the restore) and an unclean
        restart then strands the switch with no record at all.  assertLogs at
        ERROR also pins the level — a warning would not be captured."""
        c = self._coord()
        c._sell_back_to_grid_saved = True

        with patch.object(self.coord_mod.os, "replace",
                          side_effect=OSError("disk full")):
            with self.assertLogs(self.coord_mod._LOGGER,
                                 level="ERROR") as cap:
                c._persist_sell_back_to_grid_state()

        self.assertTrue(
            any("persist sell-back-to-grid" in r for r in cap.output))

    # --- FIX 6: an undeletable marker must not re-issue turn_off forever ---

    def test_undeletable_marker_falls_back_to_the_disabled_marker(self):
        """Idempotent, but noisy: without the fallback every boot writes to a
        switch for a restore that finished long ago."""
        c = self._coord(marker=True)

        with patch.object(self.coord_mod.os, "remove",
                          side_effect=PermissionError):
            with self.assertLogs(self.coord_mod._LOGGER, level="WARNING"):
                c._clear_sell_back_to_grid_state()

        self.assertIs(c._load_sell_back_to_grid_state(), False)

        # Next boot latches on the read, with no service call at all.
        asyncio.run(c._restore_sell_back_to_grid_after_restart())
        self.assertEqual(self._on(), [])
        self.assertTrue(c._sell_back_to_grid_restored)


class SellBackToGridShutdownTests(unittest.TestCase):
    """async_shutdown is a synchronous @callback and cannot await, so a clean
    unload can only get a best-effort task scheduled.  These pin that it is
    scheduled, and — just as important — that nothing depends on it: the
    startup restore is the real safety net.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, saved):
        c = self.C.__new__(self.C)
        c._unsub_pv_transitions = []
        c._unsub_listeners = []
        c._unsub_guard = None
        c._unsub_ha_started = None
        c._unsub_startup = None
        c._sell_back_to_grid_saved = saved
        # A plain coroutine function, not an AsyncMock: the assertion pins the
        # code object the shutdown handed to the event loop, which a mock
        # would answer with its own machinery.
        self.stopped = []

        async def _stop():
            self.stopped.append(True)

        c._stop_manual_selling = _stop
        c.hass = _hass_mock()
        c.hass.async_create_task = MagicMock(return_value=None)
        # A test that asserts scheduling without running it must not leave an
        # un-awaited coroutine behind.
        self.addCleanup(self._close_scheduled, c)
        return c

    @staticmethod
    def _close_scheduled(c):
        for call in c.hass.async_create_task.call_args_list:
            obj = call.args[0]
            if inspect.iscoroutine(obj):
                obj.close()

    def test_shutdown_schedules_a_stop_when_the_switch_is_ours(self):
        c = self._coord(True)

        c.async_shutdown()

        c.hass.async_create_task.assert_called_once()
        # The public entry point: nothing holds the lock here.
        scheduled = c.hass.async_create_task.call_args[0][0]
        self.assertIs(scheduled.cr_code, c._stop_manual_selling.__code__)
        # Handed over, not awaited — a shutdown that blocked would hang HA.
        self.assertEqual(inspect.getcoroutinestate(scheduled), "CORO_CREATED")
        self.assertEqual(self.stopped, [])
        asyncio.run(scheduled)
        self.assertEqual(self.stopped, [True])

    def test_shutdown_schedules_nothing_when_no_restore_is_owed(self):
        for saved in (None, False):
            with self.subTest(saved=saved):
                c = self._coord(saved)

                c.async_shutdown()

                c.hass.async_create_task.assert_not_called()

    def test_shutdown_still_releases_every_listener(self):
        """The new hook is appended, so it must not displace the teardown."""
        c = self._coord(True)
        c._unsub_listeners = [MagicMock()]
        unsub_listener = c._unsub_listeners[0]
        unsub_startup = MagicMock()
        c._unsub_startup = unsub_startup

        c.async_shutdown()

        unsub_listener.assert_called_once()
        unsub_startup.assert_called_once()
        self.assertEqual(c._unsub_listeners, [])
        self.assertIsNone(c._unsub_startup)


class ForcedSellOptionsLiveReadTests(unittest.TestCase):
    """FIX: _forced_sell_enabled was an __init__ snapshot.  _async_options_updated
    refreshes the listeners and re-runs the optimizer but never reloads the config
    entry, so a user who enabled forced sell from the options UI kept a dead tick
    until HA restarted — issue #23 cause A unfixed for exactly that user — and
    the disable-teardown was unreachable.  These pin the live read.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self, *, forced_sell):
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = True
        c._last_result = MagicMock()
        c._entry = _fake_entry(forced_sell=forced_sell)
        _tick_staged(c)
        _manual_sell_state(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_restored = True
        c._manage_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.async_create_task = MagicMock(return_value=None)
        return c

    def _scheduled_count(self, c):
        return c.hass.async_create_task.call_count

    def test_no_setup_time_snapshot_is_taken(self):
        """The pin: __init__ must not assign the flag at all, or the property is
        bypassed and the value goes stale on the next options change.  Both
        spellings — annotated and plain — because a bare assignment is the
        shape this regression actually takes."""
        source = inspect.getsource(self.C.__init__)
        for assignment in ("self._forced_sell_enabled =",
                           "self._forced_sell_enabled :",
                           "self._forced_sell_enabled:"):
            self.assertNotIn(assignment, source)

    def test_the_value_is_read_through_the_config_entry(self):
        self.assertIn("CONF_FORCED_SELL_ENABLED", inspect.getsource(self.C._forced_sell_enabled.fget))
        c = self._coord(forced_sell=True)
        self.assertTrue(c._forced_sell_enabled)
        c._entry.options[_FORCED_SELL_KEY] = False
        self.assertFalse(c._forced_sell_enabled)

    def test_an_enabled_install_has_a_live_tick(self):
        c = self._coord(forced_sell=True)
        c._forced_sell_tick_callback(None)
        self.assertEqual(self._scheduled_count(c), 1)
        c.hass.async_create_task.call_args[0][0].close()

    def test_enabling_from_the_options_ui_wakes_the_tick(self):
        """The reported symptom: options say enabled, the tick queues nothing."""
        c = self._coord(forced_sell=False)
        c._forced_sell_tick_callback(None)
        self.assertEqual(self._scheduled_count(c), 0)

        c._entry.options[_FORCED_SELL_KEY] = True   # what the flow writes
        c._forced_sell_tick_callback(None)
        self.assertEqual(self._scheduled_count(c), 1)
        c.hass.async_create_task.call_args[0][0].close()

    def test_disabling_from_the_options_ui_silences_the_tick(self):
        c = self._coord(forced_sell=True)
        c._entry.options[_FORCED_SELL_KEY] = False
        c._forced_sell_tick_callback(None)
        self.assertEqual(self._scheduled_count(c), 0)

    def test_the_evaluation_body_follows_the_options_too(self):
        """Both readers, not just the callback's guard."""
        c = self._coord(forced_sell=False)
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_not_awaited()

        c._entry.options[_FORCED_SELL_KEY] = True
        with _clock(INSIDE):
            asyncio.run(c._evaluate_forced_sell())
        c._manage_manual_selling.assert_awaited_once()


class ForcedSellTickDedupeTests(unittest.TestCase):
    """FIX: every restore holds _forced_sell_lock across its service call, so a
    wedged one stalled the tick and the 5-min timer queued another coroutine on
    top of it — 12 queued in the reproduction.  At most one at a time now.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def _coord(self):
        c = self.C.__new__(self.C)
        c._emaldo_control_enabled = True
        c._last_result = MagicMock()
        c._entry = _fake_entry(forced_sell=True)
        _tick_staged(c)
        _manual_sell_state(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_restored = True
        c._manage_manual_selling = AsyncMock()
        c.hass = _hass_mock()
        c.hass.async_create_task = MagicMock(return_value=None)
        return c

    def test_no_second_task_while_one_is_in_flight(self):
        c = self._coord()
        c._forced_sell_tick_callback(None)
        scheduled = c.hass.async_create_task.call_args[0][0]
        # Still queued, not started: the flag is set before the hand-off.
        c._forced_sell_tick_callback(None)
        self.assertEqual(c.hass.async_create_task.call_count, 1)
        asyncio.run(scheduled)

    def test_the_flag_releases_when_the_task_finishes(self):
        c = self._coord()
        c._forced_sell_tick_callback(None)
        asyncio.run(c.hass.async_create_task.call_args[0][0])
        self.assertFalse(c._forced_sell_tick_in_flight)

        c._forced_sell_tick_callback(None)
        self.assertEqual(c.hass.async_create_task.call_count, 2)
        c.hass.async_create_task.call_args[0][0].close()

    def test_the_flag_releases_even_when_the_body_raises(self):
        """Otherwise one wedged tick disables forced sell for the whole session."""
        c = self._coord()
        c._manage_manual_selling = AsyncMock(side_effect=RuntimeError("boom"))
        c._forced_sell_tick_callback(None)
        scheduled = c.hass.async_create_task.call_args[0][0]
        with self.assertRaises(RuntimeError):
            asyncio.run(scheduled)
        self.assertFalse(c._forced_sell_tick_in_flight)

    def test_the_dedupe_is_per_coordinator(self):
        """Two entries may share an Emaldo device; one entry's busy tick must not
        suppress the other's."""
        a = self._coord()
        b = self._coord()
        a._forced_sell_tick_callback(None)
        b._forced_sell_tick_callback(None)
        self.assertEqual(a.hass.async_create_task.call_count, 1)
        self.assertEqual(b.hass.async_create_task.call_count, 1)
        for coord in (a, b):
            coord.hass.async_create_task.call_args[0][0].close()


class ForcedSellMarkerScopeTests(unittest.TestCase):
    """FIX: the marker was one fixed filename while the switch is per config
    entry, so two entries sharing an Emaldo device turned each other's switch
    off and each latched off the shared marker.
    """

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator

    def test_two_entries_get_different_marker_paths(self):
        tmpdir = tempfile.mkdtemp(prefix="bo-scope-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path_a = os.path.join(
            tmpdir, self.C._sell_back_to_grid_marker_name("entry_a"))
        path_b = os.path.join(
            tmpdir, self.C._sell_back_to_grid_marker_name("entry_b"))
        self.assertNotEqual(path_a, path_b)

    def test_the_name_keeps_the_documented_convention(self):
        name = self.C._sell_back_to_grid_marker_name("abc123")
        self.assertTrue(name.startswith("battery_optimizer_sell_back_to_grid_"))
        self.assertTrue(name.endswith(".json"))

    def test_setup_scopes_the_marker_to_the_entry_id(self):
        """The wiring, not just the helper: __init__ must build the path from
        the entry it was handed."""
        source = inspect.getsource(self.C.__init__)
        self.assertIn(
            "self._sell_back_to_grid_marker_name(entry.entry_id)", source
        )


class SelfCompletionTests(unittest.TestCase):
    """Issue #23: Emaldo's manual-selling API takes the planned sell energy and
    STOPS BY ITSELF once that amount has been exported — measured, 4 kWh over an
    18:30-19:00 window finished at 18:54.  Self-completion is therefore the
    normal path.

    _manual_selling_active was set from the turn_on SUCCESS and cleared only by
    the stop path, so nothing observed the sale ending.  Two money bugs follow:
    Sell back to grid stayed on for the rest of the window (up to ~2 h of
    unrequested export), and window close sent a turn_off to interrupt a sale
    that had already finished.

    The obvious fix — "switch reads off, clear the flag" — is the hazard: the
    next tick would re-enter the start path with the window still open and open
    a SECOND sale, exporting the battery twice.  _manual_selling_served is the
    state that separates the two, and the restart tests below are the reason it
    exists.
    """

    MANUAL = "switch.power_store_manual_selling"
    SELL_BACK = "switch.power_store_sell_back_to_grid"

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.calls = []
        self.marker = _marker_path(self)

    def _coord(self, *, manual_state="on", sell_back_state="off", active=False,
               sell_slots=(30, 31, 32), seen_on=None):
        c = self.C.__new__(self.C)
        # seen_on defaults to "this tick is the first to see a sale that HAD been
        # running" - the case every test in this class is about.  The refused
        # start (never seen on) is FailedStartTests' subject.
        if seen_on is None:
            seen_on = bool(active) and manual_state == "off"
        _manual_sell_state(c, active=active, seen_on=seen_on)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=True)
        _tick_staged(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_state_path = self.marker
        c._sell_back_to_grid_restored = True
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)

        # Mutable so a test can model Emaldo finishing the sale mid-window.
        states = {self.MANUAL: manual_state, self.SELL_BACK: sell_back_state}

        def states_get(entity_id):
            if entity_id not in states:
                return None
            obj = MagicMock()
            obj.state = states[entity_id]
            return obj

        c.hass = _hass_mock()
        c.hass.states.get = MagicMock(side_effect=states_get)
        c.hass.services.async_call = self._recorder()
        c.hass.async_add_executor_job = _inline_executor()
        # A confirmed stop queues a re-plan (issue #30); close the coroutine
        # instead of leaving it un-awaited on a Mock.
        c.hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        result = MagicMock()
        result.sell_slots = list(sell_slots)
        result.sell_target_kwh = 5.0
        c._last_result = result
        self.states = states
        return c

    def _recorder(self):
        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
        return call

    def _on(self, entity_id):
        """(service, entity_id) for every call aimed at one switch."""
        return [(s, e) for _d, s, e in self.calls if e == entity_id]

    def _tick(self, c, moment=INSIDE):
        """One forced-sell tick. The age gate is re-staged against `moment` so a
        test can move the clock forward (tomorrow, a later window) without the
        plan being rejected as too old — the gate is tested elsewhere."""
        c._last_optimizer_attempt = moment - timedelta(minutes=1)
        with _clock(moment):
            asyncio.run(asyncio.wait_for(c._evaluate_forced_sell(), timeout=5))

    # --- detection ------------------------------------------------------

    def test_self_completion_restores_sell_back_before_window_end(self):
        """The sale finished; the enable we made is still on. Waiting for the
        window to close would leave up to ~2 h of unrequested export after the
        planned energy is already sold."""
        c = self._coord(manual_state="off", active=True)
        c._sell_back_to_grid_saved = True
        self._tick(c)

        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertIsNone(c._sell_back_to_grid_saved)
        self.assertFalse(c._manual_selling_active)

    def test_self_completion_never_sends_turn_off(self):
        """turn_off exists to interrupt a running sale; there is nothing left
        to interrupt, so the command is not issued at all."""
        c = self._coord(manual_state="off", active=True)
        self._tick(c)

        self.assertEqual(self._on(self.MANUAL), [])

    def test_a_still_running_sale_changes_nothing(self):
        """Reading on is the ordinary case mid-window: no restore, no flag
        change, no log-spam of stop paths."""
        c = self._coord(manual_state="on", active=True)
        c._sell_back_to_grid_saved = True
        self._tick(c)

        self.assertEqual(self.calls, [])
        self.assertTrue(c._manual_selling_active)
        self.assertIs(c._sell_back_to_grid_saved, True)

    def test_an_unreadable_state_changes_nothing(self):
        """unknown / unavailable / missing entity are all "cannot tell" — the
        tick must not guess that a running sale finished."""
        for state in ("unknown", "unavailable", None):
            with self.subTest(state=state):
                self.calls.clear()
                c = self._coord(manual_state=state, active=True)
                c._sell_back_to_grid_saved = True
                self._tick(c)

                self.assertEqual(self.calls, [])
                self.assertTrue(c._manual_selling_active)
                self.assertIs(c._sell_back_to_grid_saved, True)

    # --- the hazard: no double start ------------------------------------

    def test_no_second_sale_while_the_window_is_still_open(self):
        """THE hazard. Clearing _manual_selling_active on self-completion
        without the served-guard lets the next tick see an open window and no
        active sale, and open a second one — a double export. Measured across
        several ticks inside the window, turn_on must happen exactly once."""
        c = self._coord(manual_state="on", sell_back_state="off")
        self._tick(c)
        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])

        # Emaldo exports the planned 5 kWh and stops itself, mid-window.
        self.states[self.MANUAL] = "off"
        self.calls.clear()
        for _ in range(5):
            self._tick(c)

        self.assertEqual(
            self._on(self.MANUAL), [],
            "no second sale may be opened for an already-served window",
        )
        # Exactly one restore (the tick that noticed), not one per tick.
        self.assertEqual(
            self._on(self.SELL_BACK), [("turn_off", self.SELL_BACK)]
        )

    def test_the_sale_is_not_restarted_after_the_sell_back_restore(self):
        """Same hazard with the restore interleaved: the tick that notices the
        self-completion also turns Sell back to grid off, and the window is
        still open. The next tick must not re-enable anything."""
        c = self._coord(manual_state="on")
        self._tick(c)
        self.states[self.MANUAL] = "off"
        self.calls.clear()

        self._tick(c)   # detects completion + restores
        self.calls.clear()
        self._tick(c)   # window still open: must stay quiet

        self.assertEqual(self.calls, [])
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_a_new_window_can_be_sold_again(self):
        """The guard must not latch forever: tomorrow's window is a different
        key, so it sells normally."""
        c = self._coord(manual_state="on")
        self._tick(c)
        self.states[self.MANUAL] = "off"
        self._tick(c)
        self.assertFalse(c._manual_selling_active)

        self.states[self.MANUAL] = "on"
        self.calls.clear()
        tomorrow = INSIDE + timedelta(days=1)
        self._tick(c, tomorrow)

        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])
        self.assertTrue(c._manual_selling_active)

    def test_a_different_window_today_can_be_sold_again(self):
        """A replan that moves the window within the same day is a new window,
        not the served one."""
        c = self._coord(manual_state="on")
        self._tick(c)
        self.states[self.MANUAL] = "off"
        self._tick(c)

        self.states[self.MANUAL] = "on"
        self.calls.clear()
        c._last_result.sell_slots = [40, 41, 42]
        self._tick(c, datetime(2026, 10, 6, 10, 15))   # slot 41

        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])

    def test_the_served_key_is_day_and_window(self):
        """The guard's identity, asserted directly: same day + same window is
        the only thing that blocks a start."""
        c = self._coord(manual_state="on")
        self._tick(c)
        key = c._manual_selling_served
        self.assertEqual(key, (INSIDE.date(), (30, 31, 32)))

        c2 = self._coord(manual_state="on")
        self.assertIsNone(c2._manual_selling_served)

    # --- window close ----------------------------------------------------

    def test_window_close_interrupts_a_sale_still_running(self):
        """The interrupt case is real: the planned energy was not exported, so
        window end must turn the switch off."""
        c = self._coord(manual_state="on", active=True)
        c._sell_back_to_grid_saved = True
        self._tick(c, PAST_WINDOW)

        self.assertEqual(self._on(self.MANUAL), [("turn_off", self.MANUAL)])
        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertFalse(c._manual_selling_active)

    def test_window_close_does_not_interrupt_a_finished_sale(self):
        """Self-completion detected first, then the window closes: no turn_off."""
        c = self._coord(manual_state="off", active=True)
        c._sell_back_to_grid_saved = True
        self._tick(c)               # detects it finished, restores
        self.calls.clear()
        self._tick(c, PAST_WINDOW)  # window closes

        self.assertEqual(self._on(self.MANUAL), [])

    def test_window_close_without_a_prior_observation_sends_no_turn_off(self):
        """Same tick: the sale finished but no earlier tick saw it, so the
        stop path meets the switch already off. turn_off would be interrupting
        nothing, so it is skipped — while the restore still happens."""
        c = self._coord(manual_state="off", active=True)
        c._sell_back_to_grid_saved = True
        self._tick(c, PAST_WINDOW)

        self.assertEqual(self._on(self.MANUAL), [])
        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))

    # --- marker ----------------------------------------------------------

    def test_self_completion_clears_the_restart_marker(self):
        """No restart recovery is owed for a sale that already finished, so the
        marker must not outlive it — otherwise the next boot turns off a switch
        that is the user's again."""
        c = self._coord(manual_state="off", active=True)
        c._sell_back_to_grid_saved = True
        c._persist_sell_back_to_grid_state(True)
        self.assertTrue(os.path.exists(self.marker))

        self._tick(c)

        self.assertFalse(os.path.exists(self.marker))

    # --- no deadlock -----------------------------------------------------

    def test_the_completion_path_never_reenters_the_lock(self):
        """Every leg runs from inside _manage_manual_selling_locked, i.e.
        already holding _forced_sell_lock. asyncio.Lock is not reentrant, so a
        public entry point anywhere in there blocks forever instead of
        returning — hence the hard timeout."""
        c = self._coord(manual_state="on")
        asyncio.run(asyncio.wait_for(
            c._manage_manual_selling(c._last_result, INSIDE), timeout=5))
        self.states[self.MANUAL] = "off"
        for _ in range(3):
            asyncio.run(asyncio.wait_for(
                c._manage_manual_selling(c._last_result, INSIDE), timeout=5))

        self.assertFalse(c._manual_selling_active)
        self.assertIsNone(c._sell_back_to_grid_saved)

    def test_a_finished_sale_still_completes_through_the_stop_entry_point(self):
        """_stop_manual_selling takes the lock and delegates to the _locked
        variant; the skip decision lives in the locked body, so it holds on
        this path too."""
        c = self._coord(manual_state="off", active=True)
        c._sell_back_to_grid_saved = True
        asyncio.run(asyncio.wait_for(c._stop_manual_selling(), timeout=5))

        self.assertEqual(self._on(self.MANUAL), [])
        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))

    def test_switch_id_falls_back_when_resolution_fails(self):
        """Auto-discovery can miss (the Emaldo registry is still empty on the
        first tick after boot); the documented fallback is used, uncached."""
        c = self._coord(manual_state="on")
        c._resolve_emaldo_entity = MagicMock(return_value=None)
        self.assertEqual(c._manual_selling_switch_id(), self.MANUAL)


class FailedStartTests(unittest.TestCase):
    """Issue #23 follow-up: switch.turn_on does NOT raise when Emaldo refuses to
    start the sale.

    The reporter's own measured evidence is that "Manual selling" read back off
    for three minutes with the battery flat at house load and no export.  So a
    refused start and a sale that finished itself both present as "the switch
    reads off" - and the self-completion check concluded the latter.  The window
    was then stamped served with nothing sold: the same money bug issue #23
    exists to fix, moved rather than removed.

    The discriminator is whether the sale was ever OBSERVED running, so these
    tests pin both halves: off-after-on is a completion, off-without-on is a
    failed start that must be retried and must never consume the window.
    """

    MANUAL = "switch.power_store_manual_selling"
    SELL_BACK = "switch.power_store_sell_back_to_grid"

    def setUp(self):
        self.coord_mod, self.const = load_coordinator()
        self.C = self.coord_mod.BatteryOptimizerCoordinator
        self.GRACE = self.coord_mod._MANUAL_SELLING_START_GRACE_S
        self.MAX_ATTEMPTS = self.coord_mod._MANUAL_SELLING_MAX_START_ATTEMPTS
        self.calls = []
        self.marker = _marker_path(self)

    def _coord(self, *, sell_back_state="off", sell_slots=(30, 31, 32)):
        """A coordinator whose manual-selling switch always reads off, i.e. the
        refusal this issue is about.  turn_on is recorded but changes nothing,
        which is precisely the shape of the measured failure.  Sell back to grid
        reads as the user left it, so the start path's prerequisite is in place
        and the refusal is about the sale alone."""
        c = self.C.__new__(self.C)
        _manual_sell_state(c)
        c._emaldo_control_enabled = True
        c._forced_sell_lock = asyncio.Lock()
        c._entry = _fake_entry(forced_sell=True)
        _tick_staged(c)
        c._sell_back_to_grid_saved = None
        c._sell_back_to_grid_switch_id_cached = None
        c._sell_back_to_grid_state_path = self.marker
        c._sell_back_to_grid_restored = True
        c._resolve_emaldo_entity = MagicMock(
            side_effect=lambda key, domain="sensor": f"{domain}.power_store_{key}")
        c.resolve_emaldo_device = MagicMock(return_value=True)

        self.states = {self.MANUAL: "off", self.SELL_BACK: sell_back_state}

        def states_get(entity_id):
            if entity_id not in self.states:
                return None
            obj = MagicMock()
            obj.state = self.states[entity_id]
            return obj

        c.hass = _hass_mock()
        c.hass.states.get = MagicMock(side_effect=states_get)
        c.hass.services.async_call = self._recorder()
        c.hass.async_add_executor_job = _inline_executor()
        c.hass.async_create_task = MagicMock(return_value=None)

        result = MagicMock()
        result.sell_slots = list(sell_slots)
        result.sell_target_kwh = 5.0
        c._last_result = result
        return c

    def _recorder(self):
        async def call(domain, service, data=None, **kwargs):
            self.calls.append((domain, service, (data or {}).get("entity_id")))
        return call

    def _on(self, entity_id):
        return [(s, e) for _d, s, e in self.calls if e == entity_id]

    def _tick(self, c, moment):
        """One forced-sell tick at `moment`; the age gate is re-staged against it
        so advancing the clock is not rejected as a stale plan."""
        c._last_optimizer_attempt = moment - timedelta(minutes=1)
        with _clock(moment):
            asyncio.run(asyncio.wait_for(c._evaluate_forced_sell(), timeout=5))

    def _window_key(self, c, moment=INSIDE):
        return (moment.date(), tuple(c._last_result.sell_slots))

    # --- observed on, then off: the genuine completion -------------------

    def test_on_then_off_is_treated_as_completed(self):
        """The sale ran and exported the planned energy: flag cleared, window
        stamped served, Sell back to grid restored, and no turn_off - turn_off
        exists to interrupt a sale still running, and there is none."""
        c = self._coord()
        self.states[self.MANUAL] = "on"
        self._tick(c, INSIDE)
        self.assertTrue(c._manual_selling_seen_on)
        self.assertEqual(c._manual_selling_served, self._window_key(c))
        self.assertIs(c._sell_back_to_grid_saved, True)

        # Emaldo stops itself once the staged target is exported.
        self.states[self.MANUAL] = "off"
        self.calls.clear()
        self._tick(c, INSIDE + timedelta(minutes=5))

        self.assertFalse(c._manual_selling_active)
        self.assertEqual(c._manual_selling_served, self._window_key(c))
        self.assertIn(("turn_off", self.SELL_BACK), self._on(self.SELL_BACK))
        self.assertEqual(self._on(self.MANUAL), [])

    # --- off without ever having been on: the failed start ----------------

    def test_a_refused_start_is_not_treated_as_completed(self):
        """The defect. turn_on did not raise, the switch reads off, and the sale
        was never observed running - so nothing was exported. The window must
        not be stamped served and the flag must not be cleared as if it had."""
        c = self._coord()
        self._tick(c, INSIDE)

        self.assertTrue(c._manual_selling_active)
        self.assertFalse(c._manual_selling_seen_on)
        self.assertIsNone(
            c._manual_selling_served,
            "a refused start must not consume the window",
        )
        # The sale never ran, so there is nothing to recover across a
        # restart: a marker here would make the recovery treat the window
        # as served, with nothing exported.
        self.assertFalse(
            os.path.exists(c._manual_sell_state_path),
            "a refused start must not leave a restart marker",
        )

        self.calls.clear()
        self._tick(c, INSIDE + timedelta(minutes=5))

        self.assertIsNone(
            c._manual_selling_served,
            "still not served: the sale never ran for this window",
        )

    def test_a_refused_start_is_retried_on_a_later_tick(self):
        """turn_on more than once for the SAME window, and only while that
        window is open."""
        c = self._coord()
        self._tick(c, INSIDE)
        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])

        self.calls.clear()
        self._tick(c, INSIDE + timedelta(minutes=5))

        self.assertEqual(
            self._on(self.MANUAL),
            [("turn_on", self.MANUAL)],
            "the retry must re-stage and re-issue turn_on for the same window",
        )
        # The prerequisite is deliberately left alone: the retry is for this same
        # window and needs Sell back to grid still enabled.
        self.assertIs(c._sell_back_to_grid_saved, True)

    def test_retries_are_bounded_within_the_window(self):
        """Bounded, so a switch that keeps refusing cannot burn the window on
        turn_on after turn_on."""
        c = self._coord()
        moments = [INSIDE + timedelta(minutes=5 * i) for i in range(5)]
        for moment in moments:
            self._tick(c, moment)

        starts = self._on(self.MANUAL)
        self.assertEqual(
            len(starts), self.MAX_ATTEMPTS,
            f"one start plus at most {self.MAX_ATTEMPTS - 1} bounded retries",
        )
        self.assertTrue(all(s == "turn_on" for s, _ in starts))
        self.assertIsNone(c._manual_selling_served)

    def test_a_converging_switch_is_not_declared_failed_immediately(self):
        """The grace. The start path's own read-back happens immediately after a
        blocking turn_on, but a switch that has not converged yet must not be
        written off on the strength of that single read."""
        c = self._coord()
        self._tick(c, INSIDE)
        self.assertIsNone(c._manual_selling_served)

        self.calls.clear()
        self._tick(c, INSIDE + timedelta(seconds=self.GRACE // 2))

        self.assertEqual(self.calls, [], "inside the grace nothing is re-issued")
        self.assertTrue(c._manual_selling_active)
        self.assertIsNone(c._manual_selling_served)

        # Past the grace the same reading is a failed start, and is acted on.
        self.calls.clear()
        self._tick(c, INSIDE + timedelta(seconds=self.GRACE + 5))
        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])

    def test_a_converging_switch_that_starts_is_a_normal_completion(self):
        """Same window, no retry consumed: the sale simply came up. The window is
        served and the sale is not started a second time."""
        c = self._coord()
        self._tick(c, INSIDE)
        self.states[self.MANUAL] = "on"

        self.calls.clear()
        self._tick(c, INSIDE + timedelta(seconds=self.GRACE // 2))

        self.assertEqual(self._on(self.MANUAL), [], "no retry while converging")
        self.assertTrue(c._manual_selling_seen_on)
        self.assertEqual(c._manual_selling_served, self._window_key(c))

        self.states[self.MANUAL] = "off"
        self.calls.clear()
        self._tick(c, INSIDE + timedelta(seconds=self.GRACE + 5))

        self.assertEqual(self._on(self.MANUAL), [])
        self.assertFalse(c._manual_selling_active)

    # --- the failed start must not become a double sale --------------------

    def test_a_failed_start_does_not_cause_a_double_sale(self):
        """One refused start, then the retry takes. Across the whole window
        turn_on is issued once for the refused attempt and never again: the tick
        that observes the sale running stamps the window served, so the genuine
        on->off completion that follows cannot open a second sale."""
        c = self._coord()
        self._tick(c, INSIDE)                                   # refused
        self.assertIsNone(c._manual_selling_served)

        self.states[self.MANUAL] = "on"
        self.calls.clear()
        self._tick(c, INSIDE + timedelta(minutes=5))            # the retry takes
        self.assertEqual(self._on(self.MANUAL), [], "no re-start once running")

        self.states[self.MANUAL] = "off"                        # self-completes
        self.calls.clear()
        for i in (1, 2, 3):
            self._tick(c, INSIDE + timedelta(minutes=5 * (1 + i)))

        self.assertEqual(self._on(self.MANUAL), [], "no second sale")
        self.assertFalse(c._manual_selling_active)
        self.assertEqual(c._manual_selling_served, self._window_key(c))
        self.assertEqual(
            self._on(self.SELL_BACK), [("turn_off", self.SELL_BACK)],
            "one restore, by the tick that noticed the completion",
        )

    def test_a_refused_start_does_not_block_the_next_window(self):
        """The budget is per window: tomorrow's window gets its own, even after
        this one gave up."""
        c = self._coord()
        self._tick(c, INSIDE)
        for i in range(1, 4):
            self._tick(c, INSIDE + timedelta(minutes=5 * i))
        self.assertTrue(c._manual_selling_active, "gave up, holding the flag")

        c._last_result.sell_slots = [40, 41, 42]
        self.calls.clear()
        self._tick(c, datetime(2026, 10, 7, 10, 15))           # slot 41

        self.assertEqual(self._on(self.MANUAL), [("turn_on", self.MANUAL)])
        self.assertEqual(c._manual_selling_start_attempts, 1,
                         "the new window starts with a fresh budget")

    # --- unreadable state --------------------------------------------------

    def test_an_unreadable_state_is_not_a_completion_or_a_failure(self):
        """unknown / unavailable / missing entity: cannot tell, so change
        nothing - whether or not the sale was ever seen running."""
        for seen in (True, False):
            for state in ("unknown", "unavailable", None):
                with self.subTest(seen_on=seen, state=state):
                    self.calls.clear()
                    c = self._coord()
                    _manual_sell_state(
                        c, active=True, seen_on=seen,
                        started_at=INSIDE - timedelta(hours=1),
                        attempts=self.MAX_ATTEMPTS,
                        start_window=self._window_key(c),
                    )
                    self.states[self.MANUAL] = state
                    self._tick(c, INSIDE + timedelta(minutes=5))

                    self.assertEqual(self.calls, [])
                    self.assertTrue(c._manual_selling_active)
                    self.assertEqual(c._manual_selling_start_attempts,
                                     self.MAX_ATTEMPTS)

    # --- no deadlock --------------------------------------------------------

    def test_the_retry_path_never_reenters_the_lock(self):
        """Both the retry and the give-up run from inside
        _manage_manual_selling_locked, i.e. already holding _forced_sell_lock.
        asyncio.Lock is not reentrant, so a public entry point in there blocks
        forever - hence the timeout."""
        c = self._coord()
        for i in range(5):
            moment = INSIDE + timedelta(minutes=5 * i)
            with _clock(moment):
                asyncio.run(asyncio.wait_for(
                    c._manage_manual_selling(c._last_result, moment), timeout=5))

        self.assertIsNone(c._manual_selling_served)


class CoordinatorSourceHygieneTests(unittest.TestCase):
    """coordinator.py is a mixed CRLF/LF file; two lines this feature touched
    carried a doubled CR, which compiles but shows as trailing whitespace and
    churns on the next re-save.
    """

    def test_no_doubled_carriage_returns(self):
        path = Path(load_coordinator()[0].__file__)
        self.assertNotIn(b"\r\r", path.read_bytes())


if __name__ == "__main__":
    unittest.main()
