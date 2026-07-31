"""Button platform for Battery Optimizer."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EMALDO_DOMAIN
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


class RunOptimizerButton(CoordinatorEntity[BatteryOptimizerCoordinator], ButtonEntity):
    """Button to manually trigger an optimization run."""

    _attr_has_entity_name = True
    _attr_name = "Run Optimizer"
    _attr_translation_key = "run_optimizer"
    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_run_optimizer"

    @property
    def device_info(self):
        """Return device info for the virtual Battery Optimizer device."""
        return self.coordinator.device_info

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Manual optimization triggered via button")
        await self.coordinator.run_optimizer(reason="manual_button", force=True)


class ClearScheduleButton(CoordinatorEntity[BatteryOptimizerCoordinator], ButtonEntity):
    """Button to clear all overrides and revert to internal schedule."""

    _attr_has_entity_name = True
    _attr_name = "Clear Schedule"
    _attr_translation_key = "clear_schedule"
    _attr_icon = "mdi:delete-sweep-outline"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_clear_schedule"

    @property
    def device_info(self):
        """Return device info for the virtual Battery Optimizer device."""
        return self.coordinator.device_info

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Clear schedule triggered via button")
        if self.coordinator.hass.services.has_service(EMALDO_DOMAIN, "reset_to_internal"):
            await self.coordinator.hass.services.async_call(
                EMALDO_DOMAIN, "reset_to_internal", {}, blocking=True,
            )
            _LOGGER.info("Schedule cleared (reset to internal)")
        else:
            _LOGGER.warning("Emaldo reset_to_internal service not available")
