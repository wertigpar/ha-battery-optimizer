"""Greedy battery optimizer.

Produces a 96-slot charge/discharge schedule that maximises savings
from self-consumption (avoiding grid purchases) and round-trip
arbitrage, accounting for solar, battery wear, and efficiency losses.

The Emaldo battery load-matches during discharge — it covers household
load only and does not export to grid.  Discharge value therefore
equals the grid buy price avoided, not the sell/export price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .const import (
    SLOT_NO_OVERRIDE,
    SLOT_IDLE,
    SLOTS_PER_DAY,
    SLOT_DURATION_HOURS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class BatteryConfig:
    """Battery and fee parameters."""

    capacity_kwh: float = 5.0
    max_charge_kw: float = 2.5
    max_discharge_kw: float = 2.5
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    soc_min: float = 20.0      # percent
    soc_max: float = 100.0     # percent

    vat_multiplier: float = 1.255
    transfer_fee_buy: float = 0.0572   # €/kWh
    sales_commission: float = 0.002    # €/kWh

    base_load_kw: float = 0.5

    battery_price: float = 9000.0           # € purchase cost
    battery_lifetime_cycles: int = 10000    # full charge-discharge cycles
    idle_power_kw: float = 0.1              # battery unit idle consumption (kW)

    @property
    def wear_cost_per_kwh(self) -> float:
        """Battery degradation cost per kWh cycled (€/kWh, full round-trip)."""
        if self.battery_lifetime_cycles <= 0 or self.capacity_kwh <= 0:
            return 0.0
        return self.battery_price / (self.battery_lifetime_cycles * self.capacity_kwh)

    @property
    def usable_kwh(self) -> float:
        """Usable energy range in kWh."""
        return self.capacity_kwh * (self.soc_max - self.soc_min) / 100.0

    @property
    def round_trip_factor(self) -> float:
        """Energy kept after one charge-discharge cycle."""
        return self.charge_efficiency * self.discharge_efficiency

    @property
    def max_charge_per_slot_kwh(self) -> float:
        return self.max_charge_kw * SLOT_DURATION_HOURS

    @property
    def max_discharge_per_slot_kwh(self) -> float:
        return self.max_discharge_kw * SLOT_DURATION_HOURS

    @property
    def idle_drain_per_slot_kwh(self) -> float:
        """Energy drained per slot by battery unit idle consumption."""
        return self.idle_power_kw * SLOT_DURATION_HOURS


@dataclass
class SlotPlan:
    """Plan for a single 15-minute slot."""

    index: int
    action: str          # "charge", "discharge", "idle", "none"
    slot_value: int      # emaldo override byte (0-255)
    buy_price: float     # effective €/kWh
    sell_price: float    # effective €/kWh
    solar_kw: float = 0.0
    load_kw: float = 0.0
    soc_after: float = 0.0
    profit: float = 0.0  # estimated slot profit/cost in €


@dataclass
class OptimizationResult:
    """Result of an optimization run."""

    slots: list[SlotPlan] = field(default_factory=list)
    total_profit: float = 0.0
    charge_slots: int = 0
    discharge_slots: int = 0
    idle_slots: int = 0
    reason: str = ""

    @property
    def slot_values(self) -> list[int]:
        """96 emaldo override byte values."""
        return [s.slot_value for s in self.slots]


def compute_prices(
    spot_prices: list[float],
    cfg: BatteryConfig,
) -> tuple[list[float], list[float]]:
    """Convert spot prices to 96 x 15-min buy/sell prices (€/kWh).

    Args:
        spot_prices: Spot prices in €/kWh.  Can be:
            - 96 values (already 15-minute resolution)
            - 24 values (hourly, each expanded to 4 slots)
        cfg: Battery/fee configuration.

    Returns:
        (buy_prices, sell_prices) each 96 floats.
    """
    buy: list[float] = []
    sell: list[float] = []

    if len(spot_prices) >= SLOTS_PER_DAY:
        # Already 15-minute resolution
        for spot in spot_prices[:SLOTS_PER_DAY]:
            buy.append(spot * cfg.vat_multiplier + cfg.transfer_fee_buy)
            sell.append(max(spot - cfg.sales_commission, 0.0))
    else:
        # Hourly — expand each to 4 slots
        for spot in spot_prices[:24]:
            b = spot * cfg.vat_multiplier + cfg.transfer_fee_buy
            s = max(spot - cfg.sales_commission, 0.0)
            for _ in range(4):
                buy.append(b)
                sell.append(s)

    # Pad to 96 if input was shorter
    while len(buy) < SLOTS_PER_DAY:
        buy.append(buy[-1] if buy else 0.0)
        sell.append(sell[-1] if sell else 0.0)
    return buy[:SLOTS_PER_DAY], sell[:SLOTS_PER_DAY]


def interpolate_solar_to_15min(slots_30min: list[float]) -> list[float]:
    """Expand 48 x 30-min kW values to 96 x 15-min values (flat)."""
    result: list[float] = []
    for kw in slots_30min:
        result.append(kw)
        result.append(kw)
    while len(result) < SLOTS_PER_DAY:
        result.append(0.0)
    return result[:SLOTS_PER_DAY]


def _soc_to_charge_target(soc_max: float) -> int:
    """Convert a SoC max % to an emaldo charge slot value.

    Emaldo slot values 1-100 mean 'charge until battery reaches N%'.
    """
    return min(int(soc_max), 100)


def _soc_to_discharge_target(soc_min: float) -> int:
    """Convert a SoC min % to an emaldo discharge slot value.

    Values 129-255: discharge down to (256-value)%.
    """
    target = max(int(soc_min), 0)
    return (256 - target) & 0xFF


def optimize(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    *,
    start_slot: int = 0,
    initial_soc_pct: float | None = None,
) -> OptimizationResult:
    """Run greedy optimization over 96 slots.

    Strategy:
    1. For each slot, compute net_load = load - solar.
       Negative net_load means solar surplus (free charging).
    2. For non-solar slots, rank discharge by buy_price descending
       (avoid most expensive grid purchases via self-consumption).
    3. Discharge is profitable when buy_price > wear_cost.
    4. Round-trip trades when price spread covers losses + wear.
    5. Respect SoC constraints and round-trip efficiency.

    Args:
        buy_prices: 96 effective buy prices €/kWh.
        sell_prices: 96 effective sell prices €/kWh.
        solar_15min: 96 expected solar kW values.
        cfg: Battery and fee config.
        start_slot: First slot to plan (0-95), earlier slots get "none".
        initial_soc_pct: Current SoC %. None → use soc_min.

    Returns:
        OptimizationResult with 96 SlotPlans.
    """
    n = SLOTS_PER_DAY
    soc_min_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
    soc_max_kwh = cfg.capacity_kwh * cfg.soc_max / 100.0

    if initial_soc_pct is not None:
        current_soc_kwh = cfg.capacity_kwh * initial_soc_pct / 100.0
    else:
        _LOGGER.warning(
            "initial_soc_pct is None — defaulting to soc_min (%.0f%%). "
            "Schedule will assume near-empty battery!",
            cfg.soc_min,
        )
        current_soc_kwh = soc_min_kwh

    charge_target = _soc_to_charge_target(cfg.soc_max)

    # Step 1: Identify solar surplus slots and net load.
    # Include battery idle power (e.g. 100W) as constant drain.
    idle_drain = cfg.idle_drain_per_slot_kwh
    net_loads: list[float] = []
    for s in range(n):
        net = cfg.base_load_kw - solar_15min[s]
        net_loads.append(net)

    # Step 2: For each plannable slot, compute the "spread" —
    # profit of buying at this slot's buy price and selling at the
    # best discharge slot's sell price, or vice versa.
    # We use a simpler approach: rank slots by price and greedily assign.

    # Minimum profitable spread for round-trip:
    # buy_saved > buy_charged / (η_c * η_d) + wear_cost
    min_spread_factor = 1.0 / cfg.round_trip_factor
    wear_cost = cfg.wear_cost_per_kwh  # Full round-trip cost per kWh

    # Candidate slots for charge/discharge (only future slots)
    candidates = list(range(start_slot, n))

    # Separate solar-surplus slots (free charging)
    solar_surplus_slots: list[int] = []
    grid_slots: list[int] = []
    for s in candidates:
        if net_loads[s] < 0:
            solar_surplus_slots.append(s)
        else:
            grid_slots.append(s)

    # Sort grid slots by buy price (cheapest first for charging)
    charge_candidates = sorted(grid_slots, key=lambda s: buy_prices[s])
    # Discharge candidates: ALL plannable slots, including solar surplus.
    # The Emaldo firmware prioritises solar charging even during discharge
    # mode — excess solar charges the battery automatically.  When load
    # exceeds solar, the battery discharges to cover the gap.  Including
    # solar surplus slots prevents missing high-price discharge windows
    # due to optimistic solar forecasts.
    # Sort by buy_price descending — avoid most expensive grid buys first.
    discharge_candidates = sorted(
        candidates, key=lambda s: buy_prices[s], reverse=True
    )

    # Find profitable discharge and charge slots.
    #
    # Two distinct cases:
    # A) Existing stored energy (SoC > min) — discharge for self-consumption
    #    when the avoided grid buy price exceeds the wear cost.
    # B) Round-trip trades (buy low → discharge later to avoid expensive
    #    buy) — only worthwhile when the price spread covers round-trip
    #    losses + wear.
    profitable_charge: list[int] = []
    profitable_discharge: list[int] = []

    # Case A: discharge existing energy when the avoided grid purchase
    # (buy_price) exceeds the battery wear cost.  The battery load-matches
    # during discharge — all energy offsets grid purchases at buy_price.
    if current_soc_kwh > soc_min_kwh and discharge_candidates:
        for s in discharge_candidates:
            if buy_prices[s] > wear_cost:
                profitable_discharge.append(s)

    # Case B: round-trip charge/discharge pairs — buy cheap grid energy,
    # store it, discharge later to avoid expensive grid purchases.
    # Profitable when: buy_saved > buy_charged / round_trip + wear.
    if charge_candidates and discharge_candidates:
        best_buy_saved = buy_prices[discharge_candidates[0]]
        max_buy_for_profit = (best_buy_saved - wear_cost) * cfg.round_trip_factor

        for s in charge_candidates:
            if buy_prices[s] < max_buy_for_profit:
                profitable_charge.append(s)

        # Add round-trip discharge candidates not already covered
        if profitable_charge:
            cheapest_buy = buy_prices[profitable_charge[0]]
            min_buy_for_discharge = cheapest_buy * min_spread_factor + wear_cost
            discharge_set = set(profitable_discharge)

            for s in discharge_candidates:
                if s not in discharge_set and buy_prices[s] > min_buy_for_discharge:
                    profitable_discharge.append(s)

    # Step 3: Simulate the schedule greedily.
    # Allocate charge and discharge respecting SoC limits.

    # IDLE (0x00) behaviour: the Emaldo battery in idle mode still
    # charges from excess solar and only exports to grid once full.
    # It does NOT draw from the grid.  So IDLE = "solar-only charge".
    # We use IDLE for solar surplus slots to capture free solar energy
    # without triggering grid import.

    # Pre-compute expected solar contribution so discharge planning
    # knows how much free energy is coming.  Account for idle drain
    # (battery unit consumes idle_power_kw from stored energy).
    estimated_solar_kwh = 0.0
    _tmp_soc = current_soc_kwh
    for s in solar_surplus_slots:
        surplus_kw = -net_loads[s]
        charge_kw = min(surplus_kw, cfg.max_charge_kw)
        charge_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
        net_gain = charge_kwh - idle_drain
        if net_gain > 0 and _tmp_soc + net_gain <= soc_max_kwh:
            estimated_solar_kwh += net_gain
            _tmp_soc += net_gain

    plan_actions: dict[int, str] = {}

    # First, assign discharge slots (most expensive first).
    # Energy budget: existing usable + expected solar.  If grid
    # charging is also profitable, include the remaining headroom.
    # Subtract idle drain over remaining slots.
    remaining_slots = n - start_slot
    total_idle_drain = idle_drain * remaining_slots
    discharge_energy_available = (
        (current_soc_kwh - soc_min_kwh) + estimated_solar_kwh
        - total_idle_drain
    )
    if profitable_charge:
        remaining_headroom = soc_max_kwh - (current_soc_kwh + estimated_solar_kwh)
        discharge_energy_available += max(0.0, remaining_headroom)

    for s in profitable_discharge:
        if net_loads[s] > 0:
            # Grid slot — discharge covers household load from battery
            load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
        else:
            # Solar surplus slot — discharge is a safety net.  The battery
            # firmware will charge from excess solar; if real solar is less
            # than forecast, the battery covers the shortfall.  Budget the
            # full base-load as the worst case (no solar at all).
            load_kwh = cfg.base_load_kw * SLOT_DURATION_HOURS
        if load_kwh <= 0:
            continue
        if discharge_energy_available >= load_kwh:
            plan_actions[s] = "discharge"
            discharge_energy_available -= load_kwh
        else:
            break  # Not enough energy

    # Assign solar surplus slots as idle (battery absorbs excess solar).
    # IDLE (0x00) lets the battery absorb solar without grid draw.
    # Skip slots already assigned to discharge.
    available_soc = current_soc_kwh
    for s in solar_surplus_slots:
        if s in plan_actions:
            continue
        surplus_kw = -net_loads[s]  # positive surplus
        charge_kw = min(surplus_kw, cfg.max_charge_kw)
        charge_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
        if available_soc + charge_kwh <= soc_max_kwh:
            plan_actions[s] = "idle"
            available_soc += charge_kwh

    # Grid charging: only the deficit that solar + existing SoC cannot
    # cover for planned discharges.
    total_discharge_kwh = sum(
        min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
        for s, a in plan_actions.items() if a == "discharge"
    )
    solar_actual = available_soc - current_soc_kwh
    existing_usable = current_soc_kwh - soc_min_kwh
    grid_charge_needed = max(0.0, total_discharge_kwh - solar_actual - existing_usable)

    soc_sim = available_soc
    grid_charged = 0.0
    for s in profitable_charge:
        if s in plan_actions:
            continue
        if grid_charged >= grid_charge_needed:
            break  # Enough energy from solar + existing SoC
        charge_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
        if soc_sim + charge_kwh <= soc_max_kwh:
            plan_actions[s] = "charge"
            soc_sim += charge_kwh
            grid_charged += charge_kwh
        else:
            break  # Battery full

    # Step 4: Build the result
    result_slots: list[SlotPlan] = []
    soc = current_soc_kwh
    total_profit = 0.0
    n_charge = n_discharge = n_idle = 0

    for s in range(n):
        action = "none"
        slot_value = SLOT_NO_OVERRIDE
        profit = 0.0

        if s < start_slot:
            # Past slots — don't touch
            pass
        elif s in plan_actions:
            action = plan_actions[s]
            if action == "idle" and net_loads[s] < 0:
                # Idle with solar surplus — absorbs solar without grid draw
                slot_value = SLOT_IDLE
                charge_kwh = min(-net_loads[s], cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                net_kwh = charge_kwh - idle_drain
                soc = max(min(soc + net_kwh, soc_max_kwh), soc_min_kwh)
                n_idle += 1
            elif action == "charge":
                slot_value = charge_target
                charge_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
                soc = min(soc + charge_kwh - idle_drain, soc_max_kwh)
                profit = -buy_prices[s] * cfg.max_charge_per_slot_kwh  # cost
                n_charge += 1
            elif action == "discharge":
                if net_loads[s] > 0:
                    load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
                else:
                    # Solar surplus slot in discharge mode — firmware will
                    # charge from excess solar.  For SoC accounting, assume
                    # the solar covers load and the net SoC change is small
                    # (solar charge minus idle drain).
                    surplus_kw = min(-net_loads[s], cfg.max_charge_kw)
                    solar_kwh = surplus_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    soc = max(min(soc + solar_kwh - idle_drain, soc_max_kwh), soc_min_kwh)
                    load_kwh = 0.0
                soc = max(soc - load_kwh - idle_drain, soc_min_kwh)
                # Per-slot SoC threshold: battery discharges only while
                # SoC is above the planned post-slot level.  This protects
                # the schedule against unexpected load spikes — if a large
                # consumer (e.g. sauna) drains the battery faster than
                # planned, it stops at this slot's threshold instead of
                # emptying to soc_min.
                soc_pct_after = soc / cfg.capacity_kwh * 100.0
                slot_value = _soc_to_discharge_target(max(soc_pct_after, cfg.soc_min))
                profit = (buy_prices[s] - cfg.wear_cost_per_kwh) * load_kwh
                n_discharge += 1
            else:
                # explicit idle assigned in solar surplus allocation
                slot_value = SLOT_IDLE
                soc = max(soc - idle_drain, soc_min_kwh)
                n_idle += 1
        else:
            if s >= start_slot:
                if net_loads[s] < 0:
                    # Solar surplus slot — idle absorbs solar without grid draw
                    action = "idle"
                    slot_value = SLOT_IDLE
                    charge_kwh = min(-net_loads[s], cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    net_kwh = charge_kwh - idle_drain
                    soc = max(min(soc + net_kwh, soc_max_kwh), soc_min_kwh)
                    n_idle += 1
                else:
                    # Idle — explicitly override to idle
                    action = "idle"
                    slot_value = SLOT_IDLE
                    soc = max(soc - idle_drain, soc_min_kwh)
                    n_idle += 1

        total_profit += profit
        result_slots.append(SlotPlan(
            index=s,
            action=action,
            slot_value=slot_value,
            buy_price=buy_prices[s] if s < len(buy_prices) else 0.0,
            sell_price=sell_prices[s] if s < len(sell_prices) else 0.0,
            solar_kw=solar_15min[s] if s < len(solar_15min) else 0.0,
            load_kw=cfg.base_load_kw,
            soc_after=soc / cfg.capacity_kwh * 100.0,
            profit=profit,
        ))

    result = OptimizationResult(
        slots=result_slots,
        total_profit=total_profit,
        charge_slots=n_charge,
        discharge_slots=n_discharge,
        idle_slots=n_idle,
    )

    _LOGGER.info(
        "Optimization complete: profit=%.4f€, charge=%d, discharge=%d, "
        "idle=%d slots",
        total_profit, n_charge, n_discharge, n_idle,
    )

    return result
