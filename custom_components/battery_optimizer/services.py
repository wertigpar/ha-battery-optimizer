"""Service handlers for Battery Optimizer."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, EMALDO_DOMAIN, SLOT_NO_OVERRIDE, SLOTS_PER_DAY

_LOGGER = logging.getLogger(__name__)

SERVICE_RUN_OPTIMIZER = "run_optimizer"
SERVICE_CLEAR_SCHEDULE = "clear_schedule"

SCHEMA_RUN_OPTIMIZER = vol.Schema(
    {
        vol.Optional("reason", default="manual"): cv.string,
        vol.Optional("force", default=True): cv.boolean,
    }
)

SCHEMA_CLEAR_SCHEDULE = vol.Schema({})


async def async_handle_run_optimizer(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle run_optimizer service call."""
    reason = call.data.get("reason", "manual")
    force = call.data.get("force", True)

    entries = hass.data.get(DOMAIN, {})
    if not entries:
        _LOGGER.error("No battery_optimizer entries found in hass.data")
        return

    for entry_id, coordinator in entries.items():
        _LOGGER.info("Running optimizer for entry %s (reason=%s)", entry_id, reason)
        try:
            await coordinator.run_optimizer(reason=reason, force=force)
        except Exception:
            _LOGGER.exception("Optimizer failed for entry %s", entry_id)


async def async_handle_clear_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle clear_schedule service call — reset all overrides."""
    if not hass.services.has_service(EMALDO_DOMAIN, "reset_to_internal"):
        _LOGGER.warning("Emaldo reset_to_internal service not available")
        return

    await hass.services.async_call(
        EMALDO_DOMAIN,
        "reset_to_internal",
        {},
        blocking=True,
    )
    _LOGGER.info("Battery schedule cleared (reset to internal)")


def async_register_services(hass: HomeAssistant) -> None:
    """Register battery_optimizer services."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_OPTIMIZER):
        return

    async def handle_run_optimizer(call: ServiceCall) -> None:
        await async_handle_run_optimizer(hass, call)

    async def handle_clear_schedule(call: ServiceCall) -> None:
        await async_handle_clear_schedule(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_RUN_OPTIMIZER, handle_run_optimizer, schema=SCHEMA_RUN_OPTIMIZER
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_SCHEDULE, handle_clear_schedule, schema=SCHEMA_CLEAR_SCHEDULE
    )
    _LOGGER.info("Battery optimizer services registered")


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister battery_optimizer services when last entry is removed."""
    hass.services.async_remove(DOMAIN, SERVICE_RUN_OPTIMIZER)
    hass.services.async_remove(DOMAIN, SERVICE_CLEAR_SCHEDULE)
