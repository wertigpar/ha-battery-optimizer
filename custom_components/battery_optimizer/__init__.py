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
from homeassistant.data_entry_flow import AbortFlow

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_RULE,
    DEFAULT_RULE_LABEL,
    SUBENTRY_TYPE_DEVICE,
    DEVICE_SUBENTRY_LABEL,
    DEVICE_SUBENTRY_UNIQUE_ID,
)
from .coordinator import BatteryOptimizerCoordinator
from .rules import (
    LEVEL_DEFAULT,
    ACTION_OPTIMIZER,
    rule_from_data,
    default_rule_title,
)
from .services import async_register_services, async_unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


async def _ensure_default_rule(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create the default 'optimizer everywhere' rule subentry if absent.

    Called on every setup — recreates it even if the user deleted it, so
    the default rule is effectively non-deletable.  Also migrates an
    existing default rule whose title predates the action-suffix rename
    (``DEFAULT_RULE_LABEL (Optimizer)`` etc.).
    """
    existing = [
        sub
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_RULE
    ]
    # Detect by the same key we create with (unique_id) so a pre-existing
    # default-rule subentry is always found even if its stored `level` drifted
    # away from LEVEL_DEFAULT; otherwise we re-add `unique_id="default_rule"`
    # and HA aborts the whole setup with `already_configured` (issue #7).
    default = next(
        (
            sub
            for sub in existing
            if sub.unique_id == "default_rule"
            or dict(sub.data).get("level") == LEVEL_DEFAULT
        ),
        None,
    )
    if default is not None:
        data = dict(default.data)
        rule = rule_from_data(data)
        expected_title = default_rule_title(rule)
        # Keep the stored label in sync too (the flow derives a subentry's
        # title from rule.label on edit, so a stale label would revert).
        needs_label = data.get("label") != DEFAULT_RULE_LABEL
        if default.title != expected_title or needs_label:
            _LOGGER.info(
                "Battery Optimizer: renamed default rule subentry '%s' -> '%s'",
                default.title,
                expected_title,
            )
            if needs_label:
                data["label"] = DEFAULT_RULE_LABEL
            hass.config_entries.async_update_subentry(
                entry, default, title=expected_title, data=data
            )
        return
    from homeassistant.config_entries import ConfigSubentry

    default_data = {
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
    }
    try:
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                subentry_type=SUBENTRY_TYPE_RULE,
                title=default_rule_title(rule_from_data(default_data)),
                unique_id="default_rule",
                data=default_data,
            ),
        )
    except AbortFlow as err:
        if getattr(err, "reason", None) != "already_configured":
            raise
        _LOGGER.warning(
            "Battery Optimizer: default rule subentry already exists "
            "(unique_id='default_rule') — adopting the existing one"
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

    The device is created without a ``config_subentry_id``: passing one
    to ``async_get_or_create`` on an EXISTING device silently moves it to
    that subentry, which HA deprecates (detected-usage warning, breaks in
    2027.8) and which orphaned this integration's entities.  The subentry
    binding is instead moved explicitly with ``async_update_device``, and
    only when it actually differs.
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
        try:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    subentry_type=SUBENTRY_TYPE_DEVICE,
                    title=DEVICE_SUBENTRY_LABEL,
                    unique_id=DEVICE_SUBENTRY_UNIQUE_ID,
                    data={},
                ),
            )
        except AbortFlow as err:
            if getattr(err, "reason", None) != "already_configured":
                raise
            _LOGGER.warning(
                "Battery Optimizer: device subentry already exists "
                "(unique_id=%r) — adopting the existing one",
                DEVICE_SUBENTRY_UNIQUE_ID,
            )
        device_sub = next(
            sub
            for sub in entry.subentries.values()
            if sub.subentry_type == SUBENTRY_TYPE_DEVICE
        )
    from homeassistant.helpers import device_registry as dr

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator._emaldo_device_id)},
        name=DEVICE_SUBENTRY_LABEL,
        manufacturer="Emaldo",
        model="Optimized Battery",
    )
    # Only reassign the subentry binding when it actually differs.  Using
    # async_update_device (not async_get_or_create with config_subentry_id)
    # is the supported way to move a device between subentries — passing
    # config_subentry_id to async_get_or_create on an existing device
    # triggers a deprecation warning (breaks 2027.8) and can orphan entities.
    bound = device.config_entries_subentries.get(entry.entry_id, set())
    if bound == {device_sub.subentry_id}:
        return
    # Add first, then remove each stale binding.  A single call passing both
    # add_config_subentry_id and remove_config_subentry_id for the same
    # config entry rebuilds the subentry set from the pre-add snapshot and
    # ends up DELETING the device; two separate calls leave exactly
    # {device_sub} on both the legacy (2026.2.x) and single-owner (2026.8+)
    # device registries.
    dev_reg.async_update_device(
        device.id,
        add_config_entry_id=entry.entry_id,
        add_config_subentry_id=device_sub.subentry_id,
    )
    for stale in bound - {device_sub.subentry_id}:
        dev_reg.async_update_device(
            device.id,
            remove_config_entry_id=entry.entry_id,
            remove_config_subentry_id=stale,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Battery Optimizer from a config entry."""
    coordinator = BatteryOptimizerCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await _ensure_default_rule(hass, entry)

    # Restore the persisted cost history so the realized-cost sensors
    # publish real values right after setup (not 0.0 until the first
    # 15-minute capture tick reloads the sidecar).
    await coordinator.async_restore_cost_history()

    # Set up checkpoint & Nordpool listeners
    coordinator.async_setup_listeners()

    # Register services (idempotent — only registers once)
    async_register_services(hass)

    # Resolve the Emaldo device BEFORE platforms forward.  When resolved
    # synchronously, ensure the device container subentry exists first, so
    # every platform passes config_subentry_id to async_add_entities and
    # the device-registry v3 (2026.8+) single-owner binding is honored.
    # When it does NOT resolve synchronously, defer to homeassistant_started
    # (which reloads the entry — the else-branch then creates the subentry
    # before platforms forward).
    unsub_ha_started: Callable[[], None] | None = None

    # After HA fully starts, resolve Emaldo device and reload to link entities.
    # Battery Optimizer may set up before Emaldo stores its data in hass.data,
    # so device_info returns None on first setup.  This listener fires once
    # HA is fully started (all integrations loaded), resolves the link, and
    # triggers a clean entry reload so entities get their device association.
    # Tracks the one-time listener; cleared once homeassistant_started fires.
    # Defs MUST precede the listener registration: Python binds `def` names
    # to the enclosing function scope, so a reference before the def raises
    # UnboundLocalError.

    async def _on_home_assistant_started(_event) -> None:
        nonlocal unsub_ha_started
        unsub_ha_started = None  # one-time listener already consumed by the bus
        if coordinator._emaldo_device_id is not None:
            return  # already resolved
        if coordinator.resolve_emaldo_device():
            _LOGGER.info(
                "Battery Optimizer: Emaldo device resolved after startup — reloading"
            )
            # Deliberately NO _ensure_device_subentry here.  The reload re-runs
            # async_setup_entry, whose else-branch (device now resolved) calls
            # _ensure_device_subentry BEFORE platforms forward, so every
            # platform resolves the subentry id.  Calling it here instead
            # would create the device subentry while the listener from the
            # FIRST setup is live, firing _async_options_updated -> run_optimizer
            # mid-startup on a half-set-up entry.
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

    # Forward to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

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
