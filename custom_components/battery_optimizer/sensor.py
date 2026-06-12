"""Sensor platform for Battery Optimizer."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SLOTS_PER_DAY, SLOT_DURATION_HOURS
from .coordinator import BatteryOptimizerCoordinator, _current_slot_index
from .optimizer import OptimizationResult

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
        ScheduleChartSensor(coordinator, entry),
        AutoBaseLoadSensor(coordinator, entry),
        PlanAccuracySensor(coordinator, entry),
    ])


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


class EstimatedSavingsSensor(_BaseOptimizerSensor):
    """Estimated daily savings/profit from optimized schedule."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "€"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "estimated_savings")
        self._attr_icon = "mdi:currency-eur"

    @property
    def native_value(self) -> float | None:
        if self._result is None:
            return None
        return round(self._result.total_profit, 4)


class ScheduleChartSensor(_BaseOptimizerSensor):
    """Exposes the full schedule for dashboard visualization.

    The state is a summary string; the full plan lives in attributes.
    """

    _unrecorded_attributes = frozenset({"schedule", "soc_history"})

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
        for sp in self._result.slots:
            h = (sp.index * 15) // 60
            m = (sp.index * 15) % 60
            state, target_soc = self._slot_state_and_target(sp)
            pv_on = pv_slots[sp.index] if sp.index < len(pv_slots) else True
            slots_data.append({
                "slot": sp.index,
                "time": f"{h:02d}:{m:02d}",
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
                slots_data.append({
                    "slot": sp.index,
                    "time": f"{h:02d}:{m:02d}",
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
                    "pv_sell": not pv_on,
                })

        total = self._result.total_profit
        if tomorrow is not None:
            total += tomorrow.total_profit
        attrs: dict[str, Any] = {
            "schedule": slots_data,
            "total_profit": round(total, 4),
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
