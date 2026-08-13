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
    CONF_BATTERY_WEAR_COST,
    CONF_IDLE_POWER_KW,
    CONF_ENABLE_SOC_SAFEGUARD,
    CONF_SOC_RECOVERY_BUFFER,
    DEFAULT_ENABLE_SOC_SAFEGUARD,
    DEFAULT_SOC_RECOVERY_BUFFER_PCT,
    CONF_IDLE_STRATEGY,
    CONF_PRICE_SOURCE,
    PRICE_SOURCE_EMALDO,
    PRICE_SOURCE_SENSOR,
    DEFAULT_PRICE_SOURCE,
    CONF_SOC_GUARD_INTERVAL,
    CONF_OPTIMIZER_INTERVAL,
    CONF_EMALDO_ENTRY_ID,
    CONF_AUTO_BASE_LOAD,
    CONF_LOAD_ENERGY_SENSOR,
    CONF_ENABLE_PV_STRATEGY,
    CONF_SOLAR_SELL_MIN_FORECAST_KWH,
    CONF_SOLAR_FORECAST_MODE,
    CONF_SOLAR_FORECAST_SCALE,
    CONF_SOLAR_ACTUAL_SENSOR,
    SOLAR_FORECAST_P50,
    SOLAR_FORECAST_P10,
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
    DEFAULT_BATTERY_WEAR_COST,
    DEFAULT_IDLE_POWER_KW,
    DEFAULT_IDLE_STRATEGY,
    DEFAULT_SOC_GUARD_INTERVAL,
    DEFAULT_OPTIMIZER_INTERVAL,
    SOC_GUARD_INTERVALS,
    OPTIMIZER_INTERVALS,
    IDLE_FULL_CONTROL,
    IDLE_SOLAR_GUARD,
    IDLE_SMART_OVERRIDE,
    DEFAULT_AUTO_BASE_LOAD,
    DEFAULT_LOAD_ENERGY_SENSOR,
    DEFAULT_ENABLE_PV_STRATEGY,
    DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH,
    DEFAULT_SOLAR_FORECAST_MODE,
    DEFAULT_SOLAR_FORECAST_SCALE,
    DEFAULT_SOLAR_ACTUAL_SENSOR,
    SOLAR_SCALE_MAX,
)

_LOGGER = logging.getLogger(__name__)


def _get_emaldo_options(hass) -> dict[str, str]:
    """Return {entry_id: display_label} for all loaded Emaldo entries."""
    emaldo_data = hass.data.get("emaldo") or {}
    options: dict[str, str] = {}
    for entry_id, entry_data in emaldo_data.items():
        # Skip internal shared-data keys (e.g. _home_primaries,
        # _home_secrets, _device_sessions) stored alongside real
        # config-entry data in hass.data[DOMAIN].
        if entry_id.startswith("_"):
            continue
        coord = entry_data.get("coordinator") if isinstance(entry_data, dict) else None
        home_id = getattr(coord, "home_id", None) if coord else None
        label = home_id or entry_id
        options[entry_id] = label
    return options


def _build_schema(
    defaults: dict[str, Any] | None = None,
    emaldo_options: dict[str, str] | None = None,
) -> vol.Schema:
    """Build the config / options schema with optional defaults."""
    d = defaults or {}

    def _int_default(key: str, fallback: int) -> int:
        value = d.get(key, fallback)
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _interval_schema(values: list[int]) -> dict[str, str]:
        return {str(value): f"{value} minutes" for value in values}

    emaldo_field: dict = {}
    if emaldo_options:
        emaldo_field = {
            vol.Required(
                CONF_EMALDO_ENTRY_ID,
                default=d.get(CONF_EMALDO_ENTRY_ID, next(iter(emaldo_options))),
            ): vol.In(emaldo_options)
        }
    price_source = d.get(CONF_PRICE_SOURCE, DEFAULT_PRICE_SOURCE)
    # CONF_SPOT_SENSOR is only required when price source is "sensor"
    spot_sensor_field: dict
    if price_source == PRICE_SOURCE_SENSOR:
        spot_sensor_field = {
            vol.Required(
                CONF_SPOT_SENSOR,
                default=d.get(CONF_SPOT_SENSOR, "sensor.electricity_prices"),
            ): str
        }
    else:
        spot_sensor_field = {
            vol.Optional(
                CONF_SPOT_SENSOR,
                default=d.get(CONF_SPOT_SENSOR, ""),
            ): str
        }
    return vol.Schema(
        {
            **emaldo_field,
            vol.Required(
                CONF_PRICE_SOURCE,
                default=price_source,
            ): vol.In([PRICE_SOURCE_EMALDO, PRICE_SOURCE_SENSOR]),
            **spot_sensor_field,
            vol.Required(
                CONF_SOLCAST_TODAY,
                default=d.get(CONF_SOLCAST_TODAY, "sensor.solcast_pv_forecast_forecast_today"),
            ): str,
            vol.Required(
                CONF_SOLCAST_TOMORROW,
                default=d.get(CONF_SOLCAST_TOMORROW, "sensor.solcast_pv_forecast_forecast_tomorrow"),
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
                CONF_BATTERY_WEAR_COST,
                default=d.get(CONF_BATTERY_WEAR_COST, DEFAULT_BATTERY_WEAR_COST),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5)),
            vol.Required(
                CONF_IDLE_POWER_KW,
                default=d.get(CONF_IDLE_POWER_KW, DEFAULT_IDLE_POWER_KW),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                CONF_ENABLE_SOC_SAFEGUARD,
                default=d.get(CONF_ENABLE_SOC_SAFEGUARD, DEFAULT_ENABLE_SOC_SAFEGUARD),
            ): bool,
            vol.Optional(
                CONF_SOC_RECOVERY_BUFFER,
                default=d.get(CONF_SOC_RECOVERY_BUFFER, DEFAULT_SOC_RECOVERY_BUFFER_PCT),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=30.0)),
            vol.Required(
                CONF_IDLE_STRATEGY,
                default=d.get(CONF_IDLE_STRATEGY, DEFAULT_IDLE_STRATEGY),
            ): vol.In(
                [IDLE_FULL_CONTROL, IDLE_SOLAR_GUARD, IDLE_SMART_OVERRIDE]
            ),
            vol.Required(
                CONF_SOC_GUARD_INTERVAL,
                default=str(
                    _int_default(
                        CONF_SOC_GUARD_INTERVAL, DEFAULT_SOC_GUARD_INTERVAL
                    )
                ),
            ): vol.In(_interval_schema(SOC_GUARD_INTERVALS)),
            vol.Required(
                CONF_OPTIMIZER_INTERVAL,
                default=str(
                    _int_default(
                        CONF_OPTIMIZER_INTERVAL, DEFAULT_OPTIMIZER_INTERVAL
                    )
                ),
            ): vol.In(_interval_schema(OPTIMIZER_INTERVALS)),
            vol.Optional(
                CONF_AUTO_BASE_LOAD,
                default=d.get(CONF_AUTO_BASE_LOAD, DEFAULT_AUTO_BASE_LOAD),
            ): bool,
            vol.Optional(
                CONF_LOAD_ENERGY_SENSOR,
                default=d.get(CONF_LOAD_ENERGY_SENSOR, DEFAULT_LOAD_ENERGY_SENSOR),
            ): str,
            vol.Optional(
                CONF_ENABLE_PV_STRATEGY,
                default=d.get(CONF_ENABLE_PV_STRATEGY, DEFAULT_ENABLE_PV_STRATEGY),
            ): bool,
            vol.Optional(
                CONF_SOLAR_SELL_MIN_FORECAST_KWH,
                default=d.get(
                    CONF_SOLAR_SELL_MIN_FORECAST_KWH,
                    DEFAULT_SOLAR_SELL_MIN_FORECAST_KWH,
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Optional(
                CONF_SOLAR_FORECAST_MODE,
                default=d.get(CONF_SOLAR_FORECAST_MODE, DEFAULT_SOLAR_FORECAST_MODE),
            ): vol.In([SOLAR_FORECAST_P50, SOLAR_FORECAST_P10]),
            vol.Optional(
                CONF_SOLAR_FORECAST_SCALE,
                default=d.get(
                    CONF_SOLAR_FORECAST_SCALE, DEFAULT_SOLAR_FORECAST_SCALE
                ),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=SOLAR_SCALE_MAX)),
            vol.Optional(
                CONF_SOLAR_ACTUAL_SENSOR,
                default=d.get(
                    CONF_SOLAR_ACTUAL_SENSOR, DEFAULT_SOLAR_ACTUAL_SENSOR
                ),
            ): str,
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
        emaldo_options = _get_emaldo_options(self.hass)

        if user_input is not None:
            # Only validate spot sensor if external sensor source is selected
            if user_input.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_SENSOR:
                sensor_id = user_input.get(CONF_SPOT_SENSOR, "")
                if not sensor_id or not self.hass.states.get(sensor_id):
                    errors["base"] = "sensor_not_found"

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Battery Optimizer",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_build_schema(emaldo_options=emaldo_options),
            errors=errors,
        )


class BatteryOptimizerOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle options for Battery Optimizer."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        emaldo_options = _get_emaldo_options(self.hass)

        if user_input is not None:
            errors: dict[str, str] = {}
            sensor_id = user_input.get(CONF_SOLAR_ACTUAL_SENSOR, "")
            if sensor_id and not self.hass.states.get(sensor_id):
                errors["base"] = "sensor_not_found"
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_build_schema(
                        {**self.config_entry.data, **self.config_entry.options},
                        emaldo_options=emaldo_options,
                    ),
                    errors=errors,
                )
            return self.async_create_entry(title="", data=user_input)

        # Merge config entry data with any existing options
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_build_schema(current, emaldo_options=emaldo_options),
        )
