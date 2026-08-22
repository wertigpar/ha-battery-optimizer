"""Sensor platform for Battery Optimizer."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SALES_COMMISSION,
    CONF_TRANSFER_FEE_BUY,
    CONF_VAT_MULTIPLIER,
    DEFAULT_SALES_COMMISSION,
    DEFAULT_TRANSFER_FEE_BUY,
    DEFAULT_VAT_MULTIPLIER,
    DOMAIN,
    SLOTS_PER_DAY,
    SLOT_DURATION_HOURS,
    SOLAR_REGIME_ENGAGE,
    SOLAR_REGIME_DISENGAGE,
    SOLAR_REGIME_DEBOUNCE_DAYS,
    currency_for_timezone,
)
from .solar_balance import solar_balance_report
from .coordinator import BatteryOptimizerCoordinator, _current_slot_index
from .optimizer import (
    OptimizationResult,
    baseline_cost_breakdown,
    emaldo_plan_cost_breakdown,
    optimizer_plan_cost_breakdown,
)
from .rules import sources_summary

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Optimizer sensors from a config entry."""
    coordinator: BatteryOptimizerCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities([
        OptimizerStatusSensor(coordinator, entry),
        LastRunSensor(coordinator, entry),
        CurrentActionSensor(coordinator, entry),
        EstimatedSavingsSensor(coordinator, entry),
        BaselineCostSensor(coordinator, entry),
        EmaldoPlanCostSensor(coordinator, entry),
        OptimizerPlanCostSensor(coordinator, entry),
        TomorrowEstimatedSavingsSensor(coordinator, entry),
        TomorrowBaselineCostSensor(coordinator, entry),
        TomorrowEmaldoPlanCostSensor(coordinator, entry),
        TomorrowOptimizerPlanCostSensor(coordinator, entry),
        EmaldoScheduleChartSensor(coordinator, entry),
        ScheduleChartSensor(coordinator, entry),
        UserScheduleChartSensor(coordinator, entry),
        AutoBaseLoadSensor(coordinator, entry),
        PlanAccuracySensor(coordinator, entry),
        SolarRegimeSensor(coordinator, entry),
        SolarBalanceSensor(coordinator, entry),
        VatMultiplierSensor(coordinator, entry),
        GridTransferFeeSensor(coordinator, entry),
        FeedInSalesCommissionSensor(coordinator, entry),
    ], config_subentry_id=coordinator._device_subentry_id())


class _BaseOptimizerSensor(CoordinatorEntity[BatteryOptimizerCoordinator], SensorEntity):
    """Base class for battery optimizer sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BatteryOptimizerCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._key = key
        self._currency = currency_for_timezone(coordinator.hass.config.time_zone)

    @property
    def device_info(self):
        """Return device info for the virtual Battery Optimizer device."""
        return self.coordinator.device_info

    @property
    def _result(self) -> OptimizationResult | None:
        return self.coordinator.last_result


class OptimizerStatusSensor(_BaseOptimizerSensor):
    """Shows the current optimizer status: idle, active, error."""


    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "status")
        self._attr_icon = "mdi:battery-sync"

    @property
    def native_value(self) -> str:
        if self._result is None:
            return "idle"
        now_slot = _current_slot_index()
        plan = self._result.slots
        if now_slot < len(plan) and plan[now_slot].action not in ("none", "idle"):
            return "active"
        return "scheduled"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"reason": self.coordinator.last_reason}
        if self._result:
            attrs["charge_slots"] = self._result.charge_slots
            attrs["discharge_slots"] = self._result.discharge_slots
            attrs["idle_slots"] = self._result.idle_slots
            attrs["safeguard_slots"] = self._result.safeguard_slots
        guard = self.coordinator.soc_guard_marker
        if guard is not None:
            attrs["soc_guard_marker"] = guard
        attrs["balancing_active"] = self.coordinator._is_balancing_active()
        return attrs


class LastRunSensor(_BaseOptimizerSensor):
    """Timestamp of the last optimization run."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "last_run")
        self._attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.last_run


class CurrentActionSensor(_BaseOptimizerSensor):
    """Current slot action: charge, discharge, idle, etc."""


    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "current_action")
        self._attr_icon = "mdi:battery-arrow-up"

    @property
    def native_value(self) -> str:
        if self._result is None:
            return "unknown"
        now_slot = _current_slot_index()
        if now_slot < len(self._result.slots):
            return self._result.slots[now_slot].action
        return "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        now_slot = _current_slot_index()
        if now_slot >= len(self._result.slots):
            return {}
        sp = self._result.slots[now_slot]
        return {
            "slot_index": now_slot,
            "slot_value": sp.slot_value,
            "buy_price": round(sp.buy_price, 4),
            "sell_price": round(sp.sell_price, 4),
            "solar_kw": round(sp.solar_kw, 3),
            "soc_after": round(sp.soc_after, 1),
        }


class _TomorrowBaseOptimizerSensor(_BaseOptimizerSensor):
    """Base for tomorrow-preview sensors using last_result_tomorrow."""

    @property
    def _result(self) -> OptimizationResult | None:
        return self.coordinator.last_result_tomorrow


class EstimatedSavingsSensor(_BaseOptimizerSensor):
    """Rest-of-day estimated savings/profit from optimized schedule.

    Value is NET savings: gross savings minus battery wear cost, so the
    headline number reflects what is actually gained.  Gross savings and
    the wear breakdown are exposed as attributes.
    """

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "estimated_savings")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:currency-eur"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.net_profit, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return {
            "gross_savings": round(self._result.total_profit, 4),
            "wear_cost": round(self._result.wear_cost_total, 4),
            "cycled_kwh": round(self._result.cycled_kwh, 3),
        }


class BaselineCostSensor(_BaseOptimizerSensor):
    """Rest-of-day estimated cost without any battery (pure grid purchase)."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "baseline_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:cash-remove"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.baseline_cost, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return baseline_cost_breakdown(self._result)


class EmaldoPlanCostSensor(_BaseOptimizerSensor):
    """Rest-of-day estimated cost following Emaldo's internal AI schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "emaldo_plan_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:robot"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.emaldo_cost, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return emaldo_plan_cost_breakdown(self._result)


class OptimizerPlanCostSensor(_BaseOptimizerSensor):
    """Rest-of-day estimated cost following the optimizer's schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "optimizer_plan_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:battery-arrow-up"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        # Net plan cost: baseline minus NET savings (wear included).
        return round(self._result.baseline_cost - self._result.net_profit, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return optimizer_plan_cost_breakdown(self._result)


class TomorrowEstimatedSavingsSensor(_TomorrowBaseOptimizerSensor):
    """Tomorrow estimated savings/profit from optimized schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "tomorrow_estimated_savings")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:currency-eur"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.net_profit, 4)


class TomorrowBaselineCostSensor(_TomorrowBaseOptimizerSensor):
    """Tomorrow estimated cost without any battery (pure grid purchase)."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "tomorrow_baseline_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:cash-remove"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.baseline_cost, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return baseline_cost_breakdown(self._result)


class TomorrowEmaldoPlanCostSensor(_TomorrowBaseOptimizerSensor):
    """Tomorrow estimated cost following Emaldo's internal AI schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "tomorrow_emaldo_plan_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:robot"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.emaldo_cost, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return emaldo_plan_cost_breakdown(self._result)


class TomorrowOptimizerPlanCostSensor(_TomorrowBaseOptimizerSensor):
    """Tomorrow estimated cost following the optimizer's schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "tomorrow_optimizer_plan_cost")
        self._attr_native_unit_of_measurement = self._currency
        self._attr_icon = "mdi:battery-arrow-up"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        # Net plan cost: baseline minus NET savings (wear included).
        return round(self._result.baseline_cost - self._result.net_profit, 4)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        return optimizer_plan_cost_breakdown(self._result)


class EmaldoScheduleChartSensor(_BaseOptimizerSensor):
    """Exposes the Emaldo AI's original schedule for dashboard visualization.

    Shows what Emaldo's internal AI planned before the optimizer overrides it.
    The state is a summary string; the full plan lives in attributes.
    """

    _unrecorded_attributes = frozenset({"schedule"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "emaldo_schedule_chart")
        self._attr_icon = "mdi:chart-timeline-variant"

    @staticmethod
    def _mode_to_state(mode: int) -> str:
        """Convert Emaldo mode integer to chart state label."""
        if mode == 1:
            return "Charge"
        if mode == -1:
            return "Discharge"
        return "Idle"

    @property
    def native_value(self) -> str:
        if self._result is None or not self._result.emaldo_modes:
            return "no_schedule"
        modes = self._result.emaldo_modes
        n_charge = sum(1 for m in modes if m == 1)
        n_discharge = sum(1 for m in modes if m == -1)
        n_idle = len(modes) - n_charge - n_discharge
        return f"{n_charge}C {n_discharge}D {n_idle}I"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None or not self._result.emaldo_modes:
            return {}
        modes = self._result.emaldo_modes
        slots_data = []
        # Allow up to 192 slots (48h rolling window)
        max_slots = min(len(modes), SLOTS_PER_DAY * 2)
        now_ha = dt_util.now()
        today_midnight = now_ha.replace(hour=0, minute=0, second=0, microsecond=0)
        for s, mode in enumerate(modes[:max_slots]):
            slot_of_day = s % SLOTS_PER_DAY
            h = (slot_of_day * 15) // 60
            m = (slot_of_day * 15) % 60
            day = 0 if s < SLOTS_PER_DAY else 1
            state = self._mode_to_state(mode)
            slot_dt = today_midnight + timedelta(days=day, minutes=slot_of_day * 15)
            # Pull price/solar from optimizer slots
            if day == 0:
                op = self._result.slots[s] if s < len(self._result.slots) else None
            else:
                tomorrow = self.coordinator.last_result_tomorrow
                idx = s - SLOTS_PER_DAY
                op = tomorrow.slots[idx] if tomorrow and idx < len(tomorrow.slots) else None
            slots_data.append({
                "slot": slot_of_day,
                "time": f"{h:02d}:{m:02d}",
                "t": slot_dt.isoformat(),
                "day": day,
                "state": state,
                "mode": mode,
                "buy": round(op.buy_price, 4) if op else 0.0,
                "sell": round(op.sell_price, 4) if op else 0.0,
                "solar": round(op.solar_kw, 3) if op else 0.0,
            })

        return {"schedule": slots_data}


class UserScheduleChartSensor(_BaseOptimizerSensor):
    """Exposes the user's schedule rules as a dashboard chart.

    Emits the full 48 h plan (today + tomorrow, 192 slots) so the chart
    overlays the other schedule charts on the same time axis. Slots a user
    rule governs carry source='user'; untouched slots carry source='optimizer'
    (drawn as zero bars by the dashboard generators, absent from the summary).
    """

    _unrecorded_attributes = frozenset({"schedule"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "user_schedule_chart")
        self._attr_icon = "mdi:calendar-clock"

    @property
    def native_value(self) -> str:
        if self.coordinator.last_sources is None:
            return "no_schedule"
        result = self._result
        bytes_ = result.slot_values if result is not None else []
        return sources_summary(self.coordinator.last_sources, bytes_)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        result = self._result
        sources = self.coordinator.last_sources or ["optimizer"] * len(result.slots)
        winners = self.coordinator.last_user_winners or []
        pv = result.thirdparty_pv_slots
        pvs = self.coordinator.last_pv_sources or []
        now_ha = dt_util.now()
        today_midnight = now_ha.replace(hour=0, minute=0, second=0, microsecond=0)
        slots_data = []
        for sp in result.slots:
            idx = sp.index
            h, m = (idx * 15) // 60, (idx * 15) % 60
            state, target_soc = ScheduleChartSensor._slot_state_and_target(sp)
            w = winners[idx] if idx < len(winners) else None
            slot_dt = today_midnight + timedelta(minutes=idx * 15)
            slots_data.append({
                "slot": idx,
                "time": f"{h:02d}:{m:02d}",
                "t": slot_dt.isoformat(),
                "day": 0,
                "action": sp.action,
                "state": state,
                "target_soc": target_soc,
                "source": sources[idx] if idx < len(sources) else "optimizer",
                "soc_target": w.soc_target if w else None,
                "pv_sell": (not pv[idx]) if idx < len(pv) else False,
                "pv_source": pvs[idx] if idx < len(pvs) else "optimizer",
            })

        tomorrow = self.coordinator.last_result_tomorrow
        if tomorrow is not None:
            tom_sources = (
                self.coordinator.last_sources_tomorrow
                or ["optimizer"] * len(tomorrow.slots)
            )
            tom_winners = self.coordinator.last_user_winners_tomorrow or []
            tom_pv = tomorrow.thirdparty_pv_slots
            tom_pvs = self.coordinator.last_pv_sources_tomorrow or []
            for sp in tomorrow.slots:
                idx = sp.index
                h, m = (idx * 15) // 60, (idx * 15) % 60
                state, target_soc = ScheduleChartSensor._slot_state_and_target(sp)
                w = tom_winners[idx] if idx < len(tom_winners) else None
                slot_dt = today_midnight + timedelta(days=1, minutes=idx * 15)
                slots_data.append({
                    "slot": idx,
                    "time": f"{h:02d}:{m:02d}",
                    "t": slot_dt.isoformat(),
                    "day": 1,
                    "action": sp.action,
                    "state": state,
                    "target_soc": target_soc,
                    "source": tom_sources[idx] if idx < len(tom_sources) else "optimizer",
                    "soc_target": w.soc_target if w else None,
                    "pv_sell": (not tom_pv[idx]) if idx < len(tom_pv) else False,
                    "pv_source": tom_pvs[idx] if idx < len(tom_pvs) else "optimizer",
                })

        return {"schedule": slots_data}


class ScheduleChartSensor(_BaseOptimizerSensor):
    """Exposes the full schedule for dashboard visualization.

    The state is a summary string; the full plan lives in attributes.
    """

    _unrecorded_attributes = frozenset({"schedule", "soc_history"})
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "schedule_chart")
        self._attr_icon = "mdi:chart-timeline-variant"

    @staticmethod
    def _slot_state_and_target(sp) -> tuple[str, int | None]:
        """Derive chart state label and target SoC % from a SlotPlan."""
        if sp.action in ("charge", "charge_floor") and 1 <= sp.slot_value <= 100:
            return "Charge", sp.slot_value
        if sp.action == "discharge" and sp.slot_value > 128:
            return "Discharge", 256 - sp.slot_value
        return "Idle", None

    @property
    def native_value(self) -> str:
        if self._result is None:
            return "no_schedule"
        return (
            f"{self._result.charge_slots}C "
            f"{self._result.discharge_slots}D "
            f"{self._result.idle_slots}I"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._result is None:
            return {}
        slots_data = []
        pv_slots = self._result.thirdparty_pv_slots
        now_ha = dt_util.now()
        today_midnight = now_ha.replace(hour=0, minute=0, second=0, microsecond=0)
        for sp in self._result.slots:
            h = (sp.index * 15) // 60
            m = (sp.index * 15) % 60
            state, target_soc = self._slot_state_and_target(sp)
            pv_on = pv_slots[sp.index] if sp.index < len(pv_slots) else True
            slot_dt = today_midnight + timedelta(minutes=sp.index * 15)
            slots_data.append({
                "slot": sp.index,
                "time": f"{h:02d}:{m:02d}",
                "t": slot_dt.isoformat(),
                "day": 0,
                "action": sp.action,
                "state": state,
                "target_soc": target_soc,
                "value": sp.slot_value,
                "buy": round(sp.buy_price, 4),
                "sell": round(sp.sell_price, 4),
                "solar": round(sp.solar_kw, 3),
                "soc": round(sp.soc_after, 1),
                "profit": round(sp.profit, 4),
                "export_kwh": round(sp.export_kwh, 4),
                "pv_sell": not pv_on,
            })

        tomorrow = self.coordinator.last_result_tomorrow
        if tomorrow is not None:
            tom_pv_slots = tomorrow.thirdparty_pv_slots
            for sp in tomorrow.slots:
                h = (sp.index * 15) // 60
                m = (sp.index * 15) % 60
                state, target_soc = self._slot_state_and_target(sp)
                pv_on = tom_pv_slots[sp.index] if sp.index < len(tom_pv_slots) else True
                slot_dt = today_midnight + timedelta(days=1, minutes=sp.index * 15)
                slots_data.append({
                    "slot": sp.index,
                    "time": f"{h:02d}:{m:02d}",
                    "t": slot_dt.isoformat(),
                    "day": 1,
                    "action": sp.action,
                    "state": state,
                    "target_soc": target_soc,
                    "value": sp.slot_value,
                    "buy": round(sp.buy_price, 4),
                    "sell": round(sp.sell_price, 4),
                    "solar": round(sp.solar_kw, 3),
                    "soc": round(sp.soc_after, 1),
                    "profit": round(sp.profit, 4),
                    "export_kwh": round(sp.export_kwh, 4),
                    "pv_sell": not pv_on,
                })

        total = self._result.total_profit
        baseline = self._result.baseline_cost
        if tomorrow is not None:
            total += tomorrow.total_profit
            baseline += tomorrow.baseline_cost
        attrs: dict[str, Any] = {
            "schedule": slots_data,
            "total_profit": round(total, 4),
            "baseline_cost": round(baseline, 4),
            "activated_time": self.coordinator.activated_time,
        }
        guard = self.coordinator.soc_guard_marker
        if guard is not None:
            attrs["soc_guard_marker"] = guard
        attrs["soc_history"] = self.coordinator.actual_soc_history
        return attrs


class AutoBaseLoadSensor(_BaseOptimizerSensor):
    """Exposes the auto-tuned base load kW for dashboard visibility.

    When auto-tune is disabled the value mirrors the configured
    ``base_load_kw``.  When enabled it shows the recorder-derived
    weekly average used by the optimizer.
    """

    _attr_native_unit_of_measurement = "kW"
    _attr_icon = "mdi:home-lightning-bolt-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "auto_base_load")

    @property
    def native_value(self) -> float | None:
        val = self.coordinator.auto_base_load_value
        return round(val, 3) if val is not None else None


class PlanAccuracySensor(_BaseOptimizerSensor):
    """Exposes plan-vs-actual accuracy for the window since the last optimizer run.

    State = discharge error (kWh): positive means battery discharged more than
    planned, negative means less.  Attributes contain the full breakdown.
    """

    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "plan_accuracy")

    @property
    def native_value(self) -> float | None:
        acc = self.coordinator.plan_accuracy
        if acc is None:
            return None
        return acc.get("discharge_error_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.coordinator.plan_accuracy or {}


class SolarRegimeSensor(_BaseOptimizerSensor):
    """Durable no-refill regime: gate flag + EWMA trend for the discharge floor.

    State = ``engaged`` / ``not_engaged`` (or ``unknown`` before the first
    optimizer run).  Attributes expose the EWMA trend, today's raw
    scaled-forecast fraction, the debounce counters and the tuning thresholds
    — so the winter/snow discharge gate is explainable and its
    engagement/disengagement is predictable (counters count down to the flip).
    """

    _attr_icon = "mdi:sun-snowflake"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "solar_regime")

    @property
    def native_value(self) -> str:
        regime = self.coordinator._solar_regime
        if regime is None:
            return "unknown"
        return "engaged" if regime.get("engaged") else "not_engaged"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        regime = self.coordinator._solar_regime
        attrs: dict[str, Any] = {
            "engage_threshold": SOLAR_REGIME_ENGAGE,
            "disengage_threshold": SOLAR_REGIME_DISENGAGE,
            "debounce_days": SOLAR_REGIME_DEBOUNCE_DAYS,
        }
        if regime is None:
            return attrs
        attrs["ewma"] = regime.get("ewma")
        attrs["forecast_fraction"] = self.coordinator._solar_regime_fraction
        attrs["low_days"] = regime.get("low_days")
        attrs["high_days"] = regime.get("high_days")
        attrs["last_updated"] = regime.get("date") or None
        attrs["band_kwh"] = self.coordinator._solar_regime_band_kwh
        return attrs


class SolarBalanceSensor(_BaseOptimizerSensor):
    """Self-sufficiency context: avg daily solar production vs base load.

    State = average daily solar production (kWh) over the trailing 7 days,
    derived from the persisted accuracy records (per-run external-counter
    deltas summed per calendar date — display/context only, never gates
    planning).  Unknown before ≥5 sampled days exist.  Attributes expose the
    base-load comparison and the usable band, so dashboards can answer "is
    the home a structural net importer/exporter and how long could a full
    battery alone cover base load?".
    """

    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:solar-power"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "solar_balance")

    @property
    def native_value(self) -> float | None:
        report = self._report
        return report.get("avg_daily_solar_kwh") if report else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"solar_source": None}
        hist = self.coordinator._accuracy_history
        if hist:
            attrs["solar_source"] = hist[-1].get("solar_source")
        report = self._report
        if not report:
            return attrs
        attrs.update(report)
        return attrs

    @property
    def _report(self) -> dict | None:
        opts = self.coordinator.config_entry.options
        band = None
        try:
            capacity = float(opts.get("battery_capacity_kwh") or 0.0)
            soc_max = float(opts.get("soc_max") or 0.0)
            soc_min = float(opts.get("soc_min") or 0.0)
            if capacity > 0.0:
                band = round(capacity * (soc_max - soc_min) / 100.0, 1)
        except (TypeError, ValueError):
            band = None
        return solar_balance_report(
            self.coordinator._accuracy_history,
            self.coordinator.auto_base_load_value,
            band,
        )


class VatMultiplierSensor(_BaseOptimizerSensor):
    """Diagnostic: configured VAT multiplier applied to import energy cost."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "vat_multiplier")

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.config_entry.options.get(
                CONF_VAT_MULTIPLIER, DEFAULT_VAT_MULTIPLIER
            )
        )


class GridTransferFeeSensor(_BaseOptimizerSensor):
    """Diagnostic: configured grid transfer fee (€/kWh) on imported energy."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "grid_transfer_fee")

    @property
    def native_unit_of_measurement(self) -> str:
        return f"{self._currency}/kWh"

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.config_entry.options.get(
                CONF_TRANSFER_FEE_BUY, DEFAULT_TRANSFER_FEE_BUY
            )
        )


class FeedInSalesCommissionSensor(_BaseOptimizerSensor):
    """Diagnostic: configured retailer sales commission (€/kWh) on feed-in."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "feed_in_sales_commission")

    @property
    def native_unit_of_measurement(self) -> str:
        return f"{self._currency}/kWh"

    @property
    def native_value(self) -> float:
        return float(
            self.coordinator.config_entry.options.get(
                CONF_SALES_COMMISSION, DEFAULT_SALES_COMMISSION
            )
        )
