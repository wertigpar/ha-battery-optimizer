"""Switch platform for Battery Optimizer — PV sell strategy toggle."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENABLE_EMALDO_CONTROL,
    CONF_ENABLE_PV_STRATEGY,
    DEFAULT_ENABLE_EMALDO_CONTROL,
    DEFAULT_ENABLE_PV_STRATEGY,
    DOMAIN,
    SUBENTRY_TYPE_RULE,
)
from .coordinator import BatteryOptimizerCoordinator
from .rules import rule_from_data, default_rule_title

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Optimizer switch entities from a config entry."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _rule_switches() -> list[RuleEnabledSwitch]:
        out = []
        for sub in entry.subentries.values():
            if sub.subentry_type == SUBENTRY_TYPE_RULE:
                out.append(RuleEnabledSwitch(coordinator, entry, sub))
        return out

    async_add_entities([
        PvStrategySwitch(coordinator, entry),
        EmaldoControlEnableSwitch(coordinator, entry),
        *_rule_switches(),
    ], config_subentry_id=coordinator._device_subentry_id())

    def _on_add_subentry(subentry):
        if subentry.subentry_type == SUBENTRY_TYPE_RULE:
            async_add_entities([RuleEnabledSwitch(coordinator, entry, subentry)],
                               config_subentry_id=coordinator._device_subentry_id())

    def _on_remove_subentry(subentry):
        # Entity is auto-removed by HA when its bound subentry disappears.
        pass

    # Backwards/forwards compatible: older HA lacks async_on_(add|remove)_subentry.
    if hasattr(entry, "async_on_add_subentry"):
        entry.async_on_unload(entry.async_on_add_subentry(_on_add_subentry))
    if hasattr(entry, "async_on_remove_subentry"):
        entry.async_on_unload(entry.async_on_remove_subentry(_on_remove_subentry))


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
    _attr_name = "PV Sell Strategy"
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

    @property
    def device_info(self):
        """Return device info for the virtual Battery Optimizer device."""
        return self.coordinator.device_info

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


class EmaldoControlEnableSwitch(
    CoordinatorEntity[BatteryOptimizerCoordinator],
    SwitchEntity,
    RestoreEntity,
):
    """Switch that enables/disables Emaldo battery control.

    When ON: the optimizer can push schedules to the battery via apply_bulk_schedule.
    When OFF: the optimizer runs but does not send any commands to Emaldo.

    This allows users to run the optimizer in "dry-run" mode for testing or
    debugging without actually affecting the battery schedule.

    State survives HA restarts via RestoreEntity.  Toggling triggers an
    immediate optimizer re-run via coordinator.set_emaldo_control_enabled().
    """

    _attr_has_entity_name = True
    _attr_name = "Emaldo Control"
    _attr_icon = "mdi:battery-lock"
    _attr_translation_key = "emaldo_control_enable"

    def __init__(
        self,
        coordinator: BatteryOptimizerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_emaldo_control_enable"
        # Initial default from config; overridden by restored state on startup.
        self._attr_is_on: bool = entry.options.get(
            CONF_ENABLE_EMALDO_CONTROL,
            entry.data.get(CONF_ENABLE_EMALDO_CONTROL, DEFAULT_ENABLE_EMALDO_CONTROL),
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous state and sync coordinator on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == "on"
            _LOGGER.debug(
                "Emaldo control enable switch: restored state '%s'", last_state.state
            )
        # Inform coordinator of the current state without triggering a re-run
        # (optimizer hasn't run yet at this point during HA startup).
        await self.coordinator.set_emaldo_control_enabled(self._attr_is_on, rerun=False)

    @property
    def is_on(self) -> bool:
        return self._attr_is_on

    @property
    def device_info(self):
        """Return device info for the virtual Battery Optimizer device."""
        return self.coordinator.device_info

    async def async_turn_on(self, **kwargs) -> None:
        """Enable Emaldo control and trigger immediate re-optimisation."""
        self._attr_is_on = True
        self.async_write_ha_state()
        await self.coordinator.set_emaldo_control_enabled(True, rerun=True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable Emaldo control and trigger immediate re-optimisation."""
        self._attr_is_on = False
        self.async_write_ha_state()
        await self.coordinator.set_emaldo_control_enabled(False, rerun=True)


class RuleEnabledSwitch(SwitchEntity):
    """Per-rule enable/pause toggle for User Schedule rules (issue #13)."""

    _attr_has_entity_name = True
    _attr_translation_key = "rule_enabled"

    def __init__(self, coordinator: BatteryOptimizerCoordinator, entry: ConfigEntry, subentry) -> None:
        self.coordinator = coordinator
        self._entry = entry
        self._subentry = subentry
        self._subentry_id = subentry.subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{self._subentry_id}_enabled"
        self._attr_config_subentry_id = self._subentry_id

    @property
    def _live_subentry(self):
        # Read live: async_update_subentry replaces the subentry object, so a
        # cached reference would go stale and show the wrong toggle state.
        return self.coordinator._entry.subentries.get(self._subentry_id)

    @property
    def is_on(self) -> bool:
        sub = self._live_subentry
        if sub is None:
            return False
        return bool(dict(sub.data).get("enabled", True))

    @property
    def name(self) -> str:
        sub = self._live_subentry
        if sub is None:
            return "Rule"
        data = dict(sub.data)
        return data.get("label") or default_rule_title(rule_from_data(data))

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.set_rule_enabled(self._subentry_id, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.set_rule_enabled(self._subentry_id, False)
        self.async_write_ha_state()
