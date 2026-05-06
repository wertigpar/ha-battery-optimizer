"""Switch platform for Battery Optimizer — PV sell strategy toggle."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ENABLE_PV_STRATEGY, DEFAULT_ENABLE_PV_STRATEGY, DOMAIN
from .coordinator import BatteryOptimizerCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Optimizer switch entities from a config entry."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PvStrategySwitch(coordinator, entry)])


class PvStrategySwitch(
    CoordinatorEntity[BatteryOptimizerCoordinator],
    SwitchEntity,
    RestoreEntity,
):
    """Switch that enables/disables the PV sell strategy.

    When ON: the optimizer may plan solar slots where third-party PV is
    disabled so that solar is exported to grid at the spot price instead
    of charging the battery.

    State survives HA restarts via RestoreEntity.  Toggling triggers an
    immediate optimizer re-run via coordinator.set_pv_strategy_enabled().
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:solar-power-variant"
    _attr_translation_key = "pv_strategy"

    def __init__(
        self,
        coordinator: BatteryOptimizerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_pv_strategy"
        # Initial default from config; overridden by restored state on startup.
        self._attr_is_on: bool = entry.options.get(
            CONF_ENABLE_PV_STRATEGY,
            entry.data.get(CONF_ENABLE_PV_STRATEGY, DEFAULT_ENABLE_PV_STRATEGY),
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous state and sync coordinator on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == "on"
            _LOGGER.debug(
                "PV strategy switch: restored state '%s'", last_state.state
            )
        # Inform coordinator of the current state without triggering a re-run
        # (optimizer hasn't run yet at this point during HA startup).
        await self.coordinator.set_pv_strategy_enabled(self._attr_is_on, rerun=False)

    @property
    def is_on(self) -> bool:
        return self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        """Enable PV sell strategy and trigger immediate re-optimisation."""
        self._attr_is_on = True
        self.async_write_ha_state()
        await self.coordinator.set_pv_strategy_enabled(True, rerun=True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable PV sell strategy and trigger immediate re-optimisation."""
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.coordinator.set_pv_strategy_enabled(False, rerun=True)
