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
    SUBENTRY_TYPE_DEVICE,
    DEVICE_SUBENTRY_LABEL,
    DEVICE_SUBENTRY_UNIQUE_ID,
)
from .coordinator import BatteryOptimizerCoordinator
from .rules import LEVEL_DEFAULT, ACTION_OPTIMIZER
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def _ensure_default_rule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create the default 'optimizer everywhere' rule subentry if absent.

    Called on every setup — recreates it even if the user deleted it, so
    the default rule is effectively non-deletable.  Also migrates an
    existing default rule whose title predates the rename to
    ``DEFAULT_RULE_LABEL``.
    """
    existing = [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_RULE
    ]
    default = next(
        (sub for sub in existing if dict(sub.data).get("level") == LEVEL_DEFAULT),
        None,
    )
    if default is not None:
        # Migrate pre-rename installs: title AND the stored data label
        # (the flow derives a subentry's title from rule.label on edit, so
        # a stale label would revert the row title to "Default").
        needs_label = dict(default.data).get("label") != DEFAULT_RULE_LABEL
        if default.title != DEFAULT_RULE_LABEL or needs_label:
            _LOGGER.info(
                "Battery Optimizer: renamed default rule subentry '%s' -> '%s'",
                default.title,
                DEFAULT_RULE_LABEL,
            )
            data = dict(default.data)
            if needs_label:
                data["label"] = DEFAULT_RULE_LABEL
            hass.config_entries.async_update_subentry(
                entry, default, title=DEFAULT_RULE_LABEL, data=data
            )
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


async def _ensure_device_subentry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BatteryOptimizerCoordinator,
) -> None:
    """Ensure a container subentry for the optimizer's virtual device.

    Creates a dedicated ``device`` subentry (parallel to the schedule
    subentries) and binds the optimizer device to it, so the device does
    not sit orphaned in the frontend's "Devices that don't belong to any
    sub-category" section.

    Idempotent: the subentry is only added when missing (recreated on
    next setup if the user deletes it), and ``async_get_or_create``
    reuses the device matching ``identifiers`` and only writes when
    something actually changes.  A user-renamed device keeps its custom
    name via ``name_by_user``.
    """
    if coordinator._emaldo_device_id is None:
        return
    from homeassistant.config_entries import ConfigSubentry

    device_sub = next(
        (
            sub
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_DEVICE
        ),
        None,
    )
    if device_sub is None:
        # async_add_subentry is a sync callback returning bool; the
        # subentry_id is auto-assigned by the framework.  Re-lookup the
        # created subentry by type (the add call returns a bool, not the
        # subentry).
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                subentry_type=SUBENTRY_TYPE_DEVICE,
                title=DEVICE_SUBENTRY_LABEL,
                unique_id=DEVICE_SUBENTRY_UNIQUE_ID,
                data={},
            ),
        )
        device_sub = next(
            sub
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_DEVICE
        )
    from homeassistant.helpers import device_registry as dr

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=device_sub.subentry_id,
        identifiers={(DOMAIN, coordinator._emaldo_device_id)},
        name=DEVICE_SUBENTRY_LABEL,
        manufacturer="Emaldo",
        model="Optimized Battery",
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
            await _ensure_device_subentry(hass, entry, coordinator)
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
    else:
        await _ensure_device_subentry(hass, entry, coordinator)

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
