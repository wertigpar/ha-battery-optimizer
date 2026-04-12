"""Config flow for Battery Optimizer."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_SPOT_SENSOR,
    CONF_SOLCAST_TODAY,
    CONF_SOLCAST_TOMORROW,
    CONF_BATTERY_SOC_SENSOR,
    CONF_VAT_MULTIPLIER,
    CONF_TRANSFER_FEE_BUY,
    CONF_SALES_COMMISSION,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_SOC_MIN,
    CONF_SOC_MAX,
    CONF_BASE_LOAD_KW,
    CONF_BATTERY_PRICE,
    CONF_BATTERY_LIFETIME_CYCLES,
    CONF_IDLE_POWER_KW,
    CONF_SOLAR_POWER_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_IDLE_STRATEGY,
    CONF_SOC_GUARD_INTERVAL,
    CONF_OPTIMIZER_INTERVAL,
    DEFAULT_VAT_MULTIPLIER,
    DEFAULT_TRANSFER_FEE_BUY,
    DEFAULT_SALES_COMMISSION,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_MAX_CHARGE_KW,
    DEFAULT_MAX_DISCHARGE_KW,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_SOC_MIN,
    DEFAULT_SOC_MAX,
    DEFAULT_BASE_LOAD_KW,
    DEFAULT_BATTERY_PRICE,
    DEFAULT_BATTERY_LIFETIME_CYCLES,
    DEFAULT_IDLE_POWER_KW,
    DEFAULT_SOLAR_POWER_SENSOR,
    DEFAULT_GRID_POWER_SENSOR,
    DEFAULT_BATTERY_POWER_SENSOR,
    DEFAULT_IDLE_STRATEGY,
    DEFAULT_SOC_GUARD_INTERVAL,
    DEFAULT_OPTIMIZER_INTERVAL,
    SOC_GUARD_INTERVALS,
    OPTIMIZER_INTERVALS,
    IDLE_FULL_CONTROL,
    IDLE_SOLAR_GUARD,
    IDLE_SMART_OVERRIDE,
)

_LOGGER = logging.getLogger(__name__)


def _build_schema(
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the config / options schema with optional defaults."""
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SPOT_SENSOR,
                default=d.get(CONF_SPOT_SENSOR, "sensor.electricity_prices"),
            ): str,
            vol.Required(
                CONF_SOLCAST_TODAY,
                default=d.get(CONF_SOLCAST_TODAY, "sensor.solcast_pv_forecast_forecast_today"),
            ): str,
            vol.Required(
                CONF_SOLCAST_TOMORROW,
                default=d.get(CONF_SOLCAST_TOMORROW, "sensor.solcast_pv_forecast_forecast_tomorrow"),
            ): str,
            vol.Required(
                CONF_BATTERY_SOC_SENSOR,
                default=d.get(CONF_BATTERY_SOC_SENSOR, "sensor.power_store_battery_soc"),
            ): str,
            vol.Required(
                CONF_VAT_MULTIPLIER,
                default=d.get(CONF_VAT_MULTIPLIER, DEFAULT_VAT_MULTIPLIER),
            ): vol.Coerce(float),
            vol.Required(
                CONF_TRANSFER_FEE_BUY,
                default=d.get(CONF_TRANSFER_FEE_BUY, DEFAULT_TRANSFER_FEE_BUY),
            ): vol.Coerce(float),
            vol.Required(
                CONF_SALES_COMMISSION,
                default=d.get(CONF_SALES_COMMISSION, DEFAULT_SALES_COMMISSION),
            ): vol.Coerce(float),
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH,
                default=d.get(CONF_BATTERY_CAPACITY_KWH, DEFAULT_BATTERY_CAPACITY_KWH),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_CHARGE_KW,
                default=d.get(CONF_MAX_CHARGE_KW, DEFAULT_MAX_CHARGE_KW),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_DISCHARGE_KW,
                default=d.get(CONF_MAX_DISCHARGE_KW, DEFAULT_MAX_DISCHARGE_KW),
            ): vol.Coerce(float),
            vol.Required(
                CONF_CHARGE_EFFICIENCY,
                default=d.get(CONF_CHARGE_EFFICIENCY, DEFAULT_CHARGE_EFFICIENCY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Required(
                CONF_DISCHARGE_EFFICIENCY,
                default=d.get(CONF_DISCHARGE_EFFICIENCY, DEFAULT_DISCHARGE_EFFICIENCY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Required(
                CONF_SOC_MIN,
                default=d.get(CONF_SOC_MIN, DEFAULT_SOC_MIN),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Required(
                CONF_SOC_MAX,
                default=d.get(CONF_SOC_MAX, DEFAULT_SOC_MAX),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            vol.Required(
                CONF_BASE_LOAD_KW,
                default=d.get(CONF_BASE_LOAD_KW, DEFAULT_BASE_LOAD_KW),
            ): vol.Coerce(float),
            vol.Required(
                CONF_BATTERY_PRICE,
                default=d.get(CONF_BATTERY_PRICE, DEFAULT_BATTERY_PRICE),
            ): vol.Coerce(float),
            vol.Required(
                CONF_BATTERY_LIFETIME_CYCLES,
                default=d.get(CONF_BATTERY_LIFETIME_CYCLES, DEFAULT_BATTERY_LIFETIME_CYCLES),
            ): vol.All(vol.Coerce(int), vol.Range(min=100)),
            vol.Required(
                CONF_IDLE_POWER_KW,
                default=d.get(CONF_IDLE_POWER_KW, DEFAULT_IDLE_POWER_KW),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                CONF_SOLAR_POWER_SENSOR,
                default=d.get(CONF_SOLAR_POWER_SENSOR, DEFAULT_SOLAR_POWER_SENSOR),
            ): str,
            vol.Optional(
                CONF_GRID_POWER_SENSOR,
                default=d.get(CONF_GRID_POWER_SENSOR, DEFAULT_GRID_POWER_SENSOR),
            ): str,
            vol.Optional(
                CONF_BATTERY_POWER_SENSOR,
                default=d.get(CONF_BATTERY_POWER_SENSOR, DEFAULT_BATTERY_POWER_SENSOR),
            ): str,
            vol.Required(
                CONF_IDLE_STRATEGY,
                default=d.get(CONF_IDLE_STRATEGY, DEFAULT_IDLE_STRATEGY),
            ): vol.In(
                [IDLE_FULL_CONTROL, IDLE_SOLAR_GUARD, IDLE_SMART_OVERRIDE]
            ),
            vol.Required(
                CONF_SOC_GUARD_INTERVAL,
                default=d.get(CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL),
            ): vol.In(SOC_GUARD_INTERVALS),
            vol.Required(
                CONF_OPTIMIZER_INTERVAL,
                default=d.get(CONF_OPTIMIZER_INTERVAL, DEFAULT_OPTIMIZER_INTERVAL),
            ): vol.In(OPTIMIZER_INTERVALS),
        }
    )


class BatteryOptimizerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Battery Optimizer."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BatteryOptimizerOptionsFlow(config_entry)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate that sensors exist
            for key in (CONF_SPOT_SENSOR, CONF_BATTERY_SOC_SENSOR):
                if not self.hass.states.get(user_input[key]):
                    errors["base"] = "sensor_not_found"
                    break

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Battery Optimizer",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(),
            errors=errors,
        )


class BatteryOptimizerOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options for Battery Optimizer."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Merge config entry data with any existing options
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current),
        )
