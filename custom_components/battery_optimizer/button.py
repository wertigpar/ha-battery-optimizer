"""Button platform for Battery Optimizer."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BatteryOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Optimizer buttons from a config entry."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        RunOptimizerButton(coordinator, entry),
        ClearScheduleButton(coordinator, entry),
    ])


class RunOptimizerButton(ButtonEntity):
    """Button to manually trigger an optimization run."""

    _attr_has_entity_name = True
    _attr_translation_key = "run_optimizer"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_run_optimizer"

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Manual optimization triggered via button")
        await self._coordinator.run_optimizer(reason="manual_button", force=True)


class ClearScheduleButton(ButtonEntity):
    """Button to clear all overrides and revert to internal schedule."""

    _attr_has_entity_name = True
    _attr_translation_key = "clear_schedule"
    _attr_icon = "mdi:delete-sweep-outline"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_clear_schedule"

    async def async_press(self) -> None:
        """Handle the button press."""
        from .const import EMALDO_DOMAIN

        _LOGGER.info("Clear schedule triggered via button")
        if self._coordinator.hass.services.has_service(EMALDO_DOMAIN, "reset_to_internal"):
            await self._coordinator.hass.services.async_call(
                EMALDO_DOMAIN, "reset_to_internal", {}, blocking=True,
            )
            _LOGGER.info("Schedule cleared (reset to internal)")
        else:
            _LOGGER.warning("Emaldo reset_to_internal service not available")
