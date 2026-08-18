"""Config flow for Battery Optimizer."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
)

from .const import (
    DOMAIN,
    SUBENTRY_TYPE_RULE,
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
from .rules import (
    UserRule,
    rule_errors,
    rule_from_data,
    LEVEL_WEEKDAY,
    LEVEL_DATE,
    LEVEL_DEFAULT,
    ACTIONS,
    PV_BEHAVIORS,
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


def _rule_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    weekday_options = [
        {"label": "Mon", "value": "0"}, {"label": "Tue", "value": "1"},
        {"label": "Wed", "value": "2"}, {"label": "Thu", "value": "3"},
        {"label": "Fri", "value": "4"}, {"label": "Sat", "value": "5"},
        {"label": "Sun", "value": "6"},
    ]
    days_default = [str(x) for x in d.get("days", [0])]
    return vol.Schema(
        {
            vol.Optional("label", default=d.get("label", "")): str,
            vol.Required(
                "level", default=d.get("level", LEVEL_WEEKDAY)
            ): vol.In([LEVEL_WEEKDAY, LEVEL_DATE, LEVEL_DEFAULT]),
            vol.Optional(
                "days", default=days_default
            ): SelectSelector(
                SelectSelectorConfig(options=weekday_options, multiple=True)
            ),
            vol.Optional(
                "start_date", default=d.get("start_date") or None
            ): vol.Any(None, str),
            vol.Optional(
                "end_date", default=d.get("end_date") or None
            ): vol.Any(None, str),
            vol.Required("start_time", default=d.get("start_time", "07:00")): str,
            vol.Required("end_time", default=d.get("end_time", "17:00")): str,
            vol.Required(
                "action", default=d.get("action", "charge")
            ): vol.In(ACTIONS),
            vol.Optional(
                "soc_target", default=d.get("soc_target")
            ): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=1, max=100))),
            vol.Required(
                "pv_sell", default=d.get("pv_sell", "inherit")
            ): vol.In(PV_BEHAVIORS),
        }
    )


class RuleSubentryFlow(ConfigSubentryFlow):
    """Create or edit a schedule rule subentry."""

    async def _shared_show_form(self, defaults: dict | None, errors: dict | None):
        return self.async_show_form(
            step_id=self.init_step or "user",
            data_schema=_rule_schema(defaults),
            errors=errors or {},
        )

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        return await self._process(user_input)

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        subentry = self._get_reconfigure_subentry()
        if user_input is None:
            return await self._shared_show_form(
                defaults=dict(subentry.data), errors=None
            )
        return await self._process(user_input, editing=subentry)

    async def _process(
        self,
        user_input: dict | None,
        editing: ConfigSubentry | None = None,
    ) -> ConfigFlowResult:
        if user_input is None:
            return await self._shared_show_form(defaults=None, errors=None)

        user_input = dict(user_input)
        raw_days = user_input.get("days")
        if raw_days is not None:
            user_input["days"] = [int(x) for x in raw_days]
        rule = rule_from_data(user_input)
        siblings = self._same_level_siblings(rule.level, editing)
        errors = rule_errors(rule, siblings)
        if errors:
            return await self._shared_show_form(
                defaults=user_input, errors={"base": "; ".join(errors)}
            )

        data = {
            "level": rule.level,
            "days": rule.days,
            "start_date": rule.start_date,
            "end_date": rule.end_date,
            "start_time": rule.start_time,
            "end_time": rule.end_time,
            "action": rule.action,
            "soc_target": rule.soc_target,
            "pv_sell": rule.pv_sell,
            "label": rule.label,
        }
        title = rule.label or f"{rule.level} {rule.start_time}-{rule.end_time}"

        if editing is not None:
            entry = self._get_entry()
            return self.async_update_and_abort(
                entry=entry, subentry=editing, data=data, title=title
            )
        return self.async_create_entry(title=title, data=data)

    def _same_level_siblings(
        self, level: str, editing: ConfigSubentry | None
    ) -> list[UserRule]:
        entry = self._get_entry()
        siblings = []
        for sub in entry.subentries.values():
            if sub.subentry_type != SUBENTRY_TYPE_RULE:
                continue
            if editing is not None and sub.subentry_id == editing.subentry_id:
                continue
            rule = rule_from_data(dict(sub.data))
            if rule.level == level:
                siblings.append(rule)
        return siblings


class BatteryOptimizerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Battery Optimizer."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BatteryOptimizerOptionsFlow(config_entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported subentry types: schedule rules."""
        return {SUBENTRY_TYPE_RULE: RuleSubentryFlow}

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
