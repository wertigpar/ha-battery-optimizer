"""Battery Optimizer integration for Home Assistant.

Reads Nordpool spot prices, Solcast PV forecasts, and battery state
from an Emaldo integration, then computes and applies an optimal
charge/discharge schedule to maximize savings.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import BatteryOptimizerCoordinator
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Battery Optimizer from a config entry."""
    coordinator = BatteryOptimizerCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

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
    async def _on_home_assistant_started(_event) -> None:
        if coordinator._emaldo_device_id is not None:
            return  # already resolved
        if coordinator.resolve_emaldo_device():
            _LOGGER.info(
                "Battery Optimizer: Emaldo device resolved after startup — reloading"
            )
            await hass.config_entries.async_reload(entry.entry_id)

    if not coordinator.resolve_emaldo_device():
        entry.async_on_unload(
            hass.bus.async_listen_once(
                "homeassistant_started", _on_home_assistant_started
            )
        )

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
    """Handle options flow update — re-create listeners with new sensor IDs."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_setup_listeners()
    _LOGGER.info("Battery Optimizer options updated, listeners refreshed")
