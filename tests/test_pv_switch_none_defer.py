"""RED test: ``_ensure_pv_switch_matches_plan`` must DEFER (not correct) when the
Emaldo third-party PV switch entity is missing / unavailable / unknown.

Guard (approved Option 1): when the resolved actual state is ``None`` the
coordinator cannot tell a real on/off state, so it must NOT treat ``None`` as
"off" and must NOT fire a spurious ``_set_pv_switch`` correction or a misleading
"PV switch mismatch" warning. It defers silently (debug only); a later tick
re-evaluates once a genuine state is available.

Harness mirrors ``tests/test_optimizer_soc_guard.py``: coord built via
``object.__new__`` with hermetic mocks (no live HA instance / event loop).

This is a LOCAL gitignored test (repo convention: tests/ is not tracked).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.battery_optimizer.coordinator import BatteryOptimizerCoordinator

_FALLBACK_ENTITY = "switch.power_store_third_party_pv"


def _make_state(state: str | None) -> MagicMock | None:
    """Return a fake HA state object, or None for a missing entity."""
    if state is None:
        return None
    st = MagicMock()
    st.state = state
    return st


def _make_coord(states_get_result) -> BatteryOptimizerCoordinator:
    """Build a hermetic coordinator with the PV-switch machinery mocked."""
    coord = object.__new__(BatteryOptimizerCoordinator)
    coord.hass = MagicMock()
    coord.hass.states.get = MagicMock(return_value=states_get_result)
    coord._resolve_emaldo_entity = MagicMock(return_value=_FALLBACK_ENTITY)
    coord._set_pv_switch = AsyncMock()
    coord._pv_switch_state = object()  # opaque sentinel; must stay untouched
    return coord


def _run(coro):
    return asyncio.run(coro)


# --- RED cases: actual state unresolved -> must NOT correct -------------------

def test_defer_when_entity_missing():
    """Entity absent from the state machine: defer, do not force a correction."""
    coord = _make_coord(_make_state(None))
    _run(coord._ensure_pv_switch_matches_plan(True))
    coord._set_pv_switch.assert_not_awaited()


def test_defer_when_state_unavailable():
    coord = _make_coord(_make_state("unavailable"))
    _run(coord._ensure_pv_switch_matches_plan(True))
    coord._set_pv_switch.assert_not_awaited()


def test_defer_when_state_unknown():
    coord = _make_coord(_make_state("unknown"))
    _run(coord._ensure_pv_switch_matches_plan(False))
    coord._set_pv_switch.assert_not_awaited()


def test_no_mismatch_warning_on_defer():
    """Deferral must not emit the 'PV switch mismatch' warning."""
    coord = _make_coord(_make_state("unavailable"))
    with patch("custom_components.battery_optimizer.coordinator._LOGGER.warning") as warn:
        _run(coord._ensure_pv_switch_matches_plan(True))
    warn.assert_not_called()


def test_cache_unchanged_on_defer():
    """Deferral must not mutate the cached PV switch state."""
    coord = _make_coord(_make_state("unavailable"))
    sentinel = coord._pv_switch_state
    _run(coord._ensure_pv_switch_matches_plan(True))
    assert coord._pv_switch_state is sentinel


def test_fallback_entity_used_when_resolve_none():
    """When entity resolution yields nothing, the hardcoded fallback is used."""
    coord = _make_coord(_make_state(None))
    coord._resolve_emaldo_entity.return_value = None
    _run(coord._ensure_pv_switch_matches_plan(True))
    coord.hass.states.get.assert_called_with(_FALLBACK_ENTITY)
    coord._set_pv_switch.assert_not_awaited()


# --- Positive control: known state + mismatch -> still corrects ---------------

def test_still_corrects_when_state_known_and_mismatched():
    """The guard must NOT over-suppress: real mismatches are still corrected."""
    coord = _make_coord(_make_state("off"))
    _run(coord._ensure_pv_switch_matches_plan(True))
    coord._set_pv_switch.assert_awaited_once_with(True)
