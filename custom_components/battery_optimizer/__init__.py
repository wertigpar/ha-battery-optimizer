"""Battery Optimizer integration for Home Assistant.

Reads Nordpool spot prices, Solcast PV forecasts, and battery state
from an Emaldo integration, then computes and applies an optimal
charge/discharge schedule to maximize savings.
"""

from __future__ import annotations

import logging
from typing import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_RULE,
    DEFAULT_RULE_LABEL,
)
from .coordinator import BatteryOptimizerCoordinator
from .rules import LEVEL_DEFAULT, ACTION_OPTIMIZER
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def _ensure_default_rule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create the default 'optimizer everywhere' rule subentry if absent.

    Called on every setup — recreates it even if the user deleted it, so
    the default rule is effectively non-deletable.
    """
    existing = [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_RULE
    ]
    if any(dict(sub.data).get("level") == LEVEL_DEFAULT for sub in existing):
        return
    from homeassistant.config_entries import ConfigSubentry

    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            subentry_type=SUBENTRY_TYPE_RULE,
            title=DEFAULT_RULE_LABEL,
            unique_id="default_rule",
            data={
                "level": LEVEL_DEFAULT,
                "days": [],
                "start_date": None,
                "end_date": None,
                "start_time": "00:00",
                "end_time": "24:00",
                "action": ACTION_OPTIMIZER,
                "soc_target": None,
                "pv_sell": "inherit",
                "label": DEFAULT_RULE_LABEL,
            },
        ),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Battery Optimizer from a config entry."""
    coordinator = BatteryOptimizerCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _ensure_default_rule(hass, entry)

    # Set up checkpoint & Nordpool listeners
    coordinator.async_setup_listeners()

    # Register services (idempotent — only registers once)
    async_register_services(hass)

    # Forward to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # After HA fully starts, resolve Emaldo device and reload to link entities.
    # Battery Optimizer may set up before Emaldo stores its data in hass.data,
    # so device_info returns None on first setup.  This listener fires once
    # HA is fully started (all integrations loaded), resolves the link, and
    # triggers a clean entry reload so entities get their device association.
    # Tracks the one-time listener; cleared once homeassistant_started fires.
    unsub_ha_started: Callable[[], None] | None = None

    async def _on_home_assistant_started(_event) -> None:
        nonlocal unsub_ha_started
        unsub_ha_started = None  # one-time listener already consumed by the bus
        if coordinator._emaldo_device_id is not None:
            return  # already resolved
        if coordinator.resolve_emaldo_device():
            _LOGGER.info(
                "Battery Optimizer: Emaldo device resolved after startup — reloading"
            )
            await hass.config_entries.async_reload(entry.entry_id)

    def _unload_ha_started() -> None:
        nonlocal unsub_ha_started
        if unsub_ha_started is not None:
            unsub_ha_started()
            unsub_ha_started = None

    if not coordinator.resolve_emaldo_device():
        unsub_ha_started = hass.bus.async_listen_once(
            "homeassistant_started", _on_home_assistant_started
        )
        entry.async_on_unload(_unload_ha_started)

    _LOGGER.info("Battery Optimizer set up successfully")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Battery Optimizer config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_shutdown()

        # If no entries left, unregister services
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
            hass.data.pop(DOMAIN)

    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options/subentry update — refresh listeners and re-run optimizer."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_setup_listeners()
    _LOGGER.info("Battery Optimizer options updated, listeners refreshed")
    if coordinator._ha_started:
        # Rules (subentries) changed — apply immediately, not at next poll.
        await coordinator.run_optimizer(reason="config_change", force=True)
