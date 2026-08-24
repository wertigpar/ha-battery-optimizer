"""Config flow for Battery Optimizer."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
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
    DateSelector,
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
    rule_summary,
    default_rule_title,
    LEVEL_WEEKDAY,
    LEVEL_DATE,
    LEVEL_DEFAULT,
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
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        IDLE_FULL_CONTROL,
                        IDLE_SOLAR_GUARD,
                        IDLE_SMART_OVERRIDE,
                    ],
                    translation_key="idle_strategy",
                )
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


# Canonical HA weekday keys — `translation_key="weekday"` makes the frontend
# localize these (e.g. Swedish "måndag"), whereas raw integer options render
# as untranslated chips (0=Mon…6=Sun). Internal storage stays int 0=Mon…6=Sun.
WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY_KEY_TO_INT = {key: i for i, key in enumerate(WEEKDAY_KEYS)}


def _rule_schema(
    defaults: dict | None = None,
    *,
    action: str | None = None,
    level: str | None = None,
) -> vol.Schema:
    d = defaults or {}
    level = level or d.get("level", LEVEL_WEEKDAY)
    weekday_options = list(WEEKDAY_KEYS)
    days_default = [WEEKDAY_KEYS[x] for x in d.get("days", [0]) if 0 <= x <= 6]
    soc_target_field: dict = {}
    if action in ("charge", "discharge"):
        soc_target_field = {
            vol.Optional(
                "soc_target", default=d.get("soc_target")
            ): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=1, max=100))),
        }
    # Only the fields relevant to the chosen level are shown; the level
    # itself was picked in the first menu step, so it is not a form field.
    fields: dict = {
        vol.Optional("label", default=d.get("label", "")): str,
        vol.Optional("enabled", default=d.get("enabled", True)): bool,
    }
    if level == LEVEL_WEEKDAY:
        fields[vol.Optional("days", default=days_default)] = SelectSelector(
            SelectSelectorConfig(
                options=weekday_options, multiple=True, translation_key="weekday"
            )
        )
    elif level == LEVEL_DATE:
        fields[
            vol.Optional("start_date", default=d.get("start_date") or None)
        ] = vol.Any(None, DateSelector())
        fields[
            vol.Optional("end_date", default=d.get("end_date") or None)
        ] = vol.Any(None, DateSelector())
    fields[vol.Required("start_time", default=d.get("start_time", "07:00"))] = str
    fields[vol.Required("end_time", default=d.get("end_time", "17:00"))] = str
    fields.update(soc_target_field)
    if action in ("charge", "discharge"):
        # PV-beteende only affects how surplus solar is handled during a
        # charge/discharge window. For idle/original/optimizer it is inert,
        # so don't show it (issue #13: avoid redundant re-selection).
        fields[
            vol.Required("pv_sell", default=d.get("pv_sell", "inherit"))
        ] = SelectSelector(
            SelectSelectorConfig(
                options=list(PV_BEHAVIORS), translation_key="pv_sell"
            )
        )
    return vol.Schema(fields)


class RuleSubentryFlow(ConfigSubentryFlow):
    """Create or edit a schedule rule subentry."""

    LEVEL_MENU = {
        "weekday": "Weekday rules",
        "date": "Date rules",
        "default": "Default rule",
    }

    ACTION_MENU = {
        "charge": "Charge to target SoC",
        "idle": "Idle",
        "discharge": "Discharge to floor SoC",
        "original": "Original schedule (battery AI)",
        "optimizer": "Optimizer schedule",
    }

    # Menu dispatch contract (data_entry_flow._async_handle_step): a menu
    # pick routes to ``async_step_<next_step_id>(None)``, and every menu
    # result's ``step_id`` must itself be an existing step method.  The
    # level menu's keys are the level steps below, which record the chosen
    # level and show the action menu; the action menu's keys are the action
    # steps (charge/idle/...), which route to the detail form.
    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Create flow: choose the rule level first."""
        return self._level_menu()

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Edit flow: choose (or confirm) the rule level."""
        return self._level_menu()

    def _level_menu(self) -> ConfigFlowResult:
        menu = dict(self.LEVEL_MENU)
        if self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            level = dict(subentry.data).get("level")
            if level in menu:
                menu[level] = f"{menu[level]} (current)"
        return self.async_show_menu(
            step_id=self.init_step or "user",
            menu_options=menu,
        )

    async def async_step_weekday(self, user_input=None) -> ConfigFlowResult:
        return self._select_level(LEVEL_WEEKDAY)

    async def async_step_date(self, user_input=None) -> ConfigFlowResult:
        return self._select_level(LEVEL_DATE)

    async def async_step_default(self, user_input=None) -> ConfigFlowResult:
        return self._select_level(LEVEL_DEFAULT)

    def _select_level(self, level: str) -> ConfigFlowResult:
        """Remember the chosen level, then show the action menu."""
        self._selected_level = level
        menu = dict(self.ACTION_MENU)
        if self.source == SOURCE_RECONFIGURE:
            subentry = self._get_reconfigure_subentry()
            action = dict(subentry.data).get("action")
            if action in menu:
                menu[action] = f"{menu[action]} (current)"
        return self.async_show_menu(
            step_id="action",
            menu_options=menu,
        )

    # The action menu's step_id must exist even though picks dispatch
    # directly to the action steps; this stub only re-shows the menu.
    async def async_step_action(self, user_input=None) -> ConfigFlowResult:
        return self._select_level(
            getattr(self, "_selected_level", None) or LEVEL_WEEKDAY
        )

    async def async_step_charge(self, user_input=None):
        return await self._detail_step("charge", user_input)

    async def async_step_idle(self, user_input=None):
        return await self._detail_step("idle", user_input)

    async def async_step_discharge(self, user_input=None):
        return await self._detail_step("discharge", user_input)

    async def async_step_original(self, user_input=None):
        return await self._detail_step("original", user_input)

    async def async_step_optimizer(self, user_input=None):
        return await self._detail_step("optimizer", user_input)

    # The detail form's step_id is ``detail_<action>``; on submit the
    # framework routes back to ``async_step_detail_<action>``, so each
    # needs a forwarding handler that funnels into the action step.
    async def async_step_detail_charge(self, user_input=None):
        return await self.async_step_charge(user_input)

    async def async_step_detail_idle(self, user_input=None):
        return await self.async_step_idle(user_input)

    async def async_step_detail_discharge(self, user_input=None):
        return await self.async_step_discharge(user_input)

    async def async_step_detail_original(self, user_input=None):
        return await self.async_step_original(user_input)

    async def async_step_detail_optimizer(self, user_input=None):
        return await self.async_step_optimizer(user_input)

    async def _detail_step(
        self, action: str, user_input: dict | None
    ) -> ConfigFlowResult:
        # The framework invokes every step method with a single positional
        # argument (data_entry_flow._async_handle_step), and routes menu
        # picks straight to ``async_step_<menu_key>`` — so the edit/create
        # distinction must come from the flow source, not a call-site kwarg.
        reconfigure = self.source == SOURCE_RECONFIGURE
        editing = self._get_reconfigure_subentry() if reconfigure else None
        defaults = dict(editing.data) if editing is not None else {}
        # Level comes from the first menu step (create or edit); on a bare
        # re-show fall back to the stored value.
        level = getattr(self, "_selected_level", None) or defaults.get(
            "level", LEVEL_WEEKDAY
        )
        if user_input is None:
            return self.async_show_form(
                step_id=f"detail_{action}",
                data_schema=_rule_schema(defaults, action=action, level=level),
                errors={},
            )

        user_input = dict(user_input)
        raw_days = user_input.get("days")
        if raw_days is not None:
            user_input["days"] = [_WEEKDAY_KEY_TO_INT[x] for x in raw_days]
        rule = rule_from_data({**user_input, "action": action, "level": level})
        siblings = self._same_level_siblings(rule.level, editing)
        errors = rule_errors(rule, siblings)
        if errors:
            return self.async_show_form(
                step_id=f"detail_{action}",
                data_schema=_rule_schema(user_input, action=action, level=level),
                errors={"base": "; ".join(errors)},
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
        # default rule keeps its stable "Default Schedule" label, with the
        # governing source appended (Optimizer / Original / manual action)
        if rule.level == LEVEL_DEFAULT:
            title = default_rule_title(rule)
        elif rule.label:
            title = f"{rule.label}: {rule_summary(rule)}"
        else:
            title = rule_summary(rule)

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
