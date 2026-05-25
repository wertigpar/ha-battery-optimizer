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

    wear_cost_per_kwh: float = 0.03  # € per kWh cycled (default 3 snt/kWh)
    idle_power_kw: float = 0.1       # battery unit idle consumption (kW)
    pv_sell_solar_margin: float = 0.95  # required solar fraction to allow PV selling
    pv_sell_min_price_spread: float = 0.0  # min sell price (€/kWh) to activate PV selling

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
    # Parallel per-slot PV switch plan (F2 — PV sell strategy).
    # True = third-party PV enabled (battery charges from solar).
    # False = third-party PV disabled (solar sells to grid at spot price).
    # Defaults to all-True (no change in behaviour until _plan_pv_sell_slots fills it).
    thirdparty_pv_slots: list[bool] = field(default_factory=lambda: [True] * 96)

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
            vat = cfg.vat_multiplier if spot >= 0.0 else 1.0  # no VAT amplification on negative spot
            buy.append(spot * vat + cfg.transfer_fee_buy)
            sell.append(spot - cfg.sales_commission)
    else:
        # Hourly — expand each to 4 slots
        for spot in spot_prices[:24]:
            vat = cfg.vat_multiplier if spot >= 0.0 else 1.0  # no VAT amplification on negative spot
            b = spot * vat + cfg.transfer_fee_buy
            s = spot - cfg.sales_commission
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


def _plan_pv_sell_slots(
    cfg: BatteryConfig,
    slots: list[SlotPlan],
    solar_15min: list[float],
    buy_prices: list[float],
    sell_prices: list[float],
    *,
    start_slot: int = 0,
) -> list[bool]:
    """Plan which solar slots should sell to grid vs charge the battery.

    Strategy: sell expensive morning solar, then let the battery charge to
    100% uninterrupted from a single cutover point onward.

    A single cutover slot T divides the day:
      [start_slot, T)  — PV off: sell to grid (if sell_price > wear_cost)
      [T, end)         — PV on:  solar charges battery (default)

    T is the LATEST moment we can start charging and still reach soc_max
    from solar alone.  By default T ≤ noon (slot 48); it is moved earlier
    only when post-noon solar is insufficient to fill the battery.

    If total available solar cannot fill the battery, no selling happens.

    Grid-charge slots (action == "charge") are never overridden.

    Returns:
        list[bool] of length 96.
        True  = third-party PV enabled  (solar charges battery — default).
        False = third-party PV disabled (solar exported to grid at spot price).
    """
    n = SLOTS_PER_DAY
    pv_slots = [True] * n

    _MIN_SOLAR_KW = 0.1
    # Selling is capped at noon by default.  Morning is when prices are
    # elevated; afternoon/midday is when the battery should charge.
    NOON_SLOT = 48  # slot 48 × 15 min = 12:00 local time

    soc_max_kwh = cfg.capacity_kwh * cfg.soc_max / 100.0

    slot_map: dict[int, SlotPlan] = {sp.index: sp for sp in slots}

    # Current SoC at the start of the plan window.
    first_sp = slot_map.get(start_slot)
    current_soc_kwh = (first_sp.soc_after * cfg.capacity_kwh / 100.0) if first_sp else cfg.capacity_kwh * cfg.soc_min / 100.0

    # How much net solar the battery still needs to reach soc_max.
    needed_kwh = max(0.0, soc_max_kwh - current_soc_kwh)

    if needed_kwh < 0.01:
        # Battery is already full — hardware exports excess automatically.
        return pv_slots

    # Compute cumulative net-solar available from each slot onward.
    # Only count slots where solar genuinely charges the battery
    # (net surplus after base load, not grid-charge slots).
    remaining_solar: list[float] = [0.0] * (n + 1)
    for s in range(n - 1, -1, -1):
        sp = slot_map.get(s)
        charge_kwh = 0.0
        if solar_15min[s] >= _MIN_SOLAR_KW and (sp is None or sp.action != "charge"):
            net_kw = solar_15min[s] - cfg.base_load_kw
            if net_kw > 0.0:
                charge_kw = min(net_kw, cfg.max_charge_kw)
                charge_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
        remaining_solar[s] = remaining_solar[s + 1] + charge_kwh

    # If total solar (from start_slot onward) cannot fill the battery,
    # selling would only make things worse — skip.
    if remaining_solar[start_slot] < needed_kwh * cfg.pv_sell_solar_margin:
        return pv_slots

    # Find the latest cutover T ≤ noon where solar from T onward ≥ needed_kwh.
    # If post-noon solar is insufficient, push the cutover earlier.
    effective_noon = min(NOON_SLOT, n)
    if remaining_solar[effective_noon] >= needed_kwh:
        T_cutover = effective_noon  # sell everything up to noon
    else:
        # Post-noon solar not enough — scan backward from noon to find T
        T_cutover = start_slot  # fallback: no selling
        for T in range(effective_noon, start_slot - 1, -1):
            if remaining_solar[T] >= needed_kwh:
                T_cutover = T
                break

    # No pre-cutover window to sell (e.g. already past noon).
    if T_cutover <= start_slot:
        return pv_slots

    # Mark all solar slots before the cutover as sell (if profitable).
    # Note: selling PV directly to grid incurs zero battery wear, so the
    # threshold is 0.0 (sell at any positive price).  wear_cost_per_kwh is
    # only relevant for round-trip battery charge/discharge.
    for s in range(start_slot, T_cutover):
        sp = slot_map.get(s)
        if solar_15min[s] < _MIN_SOLAR_KW:
            continue
        if sp is not None and sp.action == "charge":
            continue
        if sell_prices[s] > cfg.pv_sell_min_price_spread:  # PV sell has no battery wear cost
            pv_slots[s] = False

    cutover_h = (T_cutover * 15) // 60
    cutover_m = (T_cutover * 15) % 60
    n_sell = pv_slots.count(False)
    if n_sell:
        _LOGGER.info(
            "PV sell strategy: %d sell slots before %02d:%02d, "
            "battery needs %.2f kWh from solar (%.2f kWh available after cutover)",
            n_sell, cutover_h, cutover_m, needed_kwh, remaining_solar[T_cutover],
        )
    else:
        _LOGGER.debug(
            "PV sell strategy: no sell slots found "
            "(start=%d, T_cutover=%d, needed=%.2f kWh, remaining@noon=%.2f kWh)",
            start_slot, T_cutover, needed_kwh, remaining_solar[min(NOON_SLOT, n)],
        )
    return pv_slots


def _correct_soc_for_pv_sells(
    slots: list[SlotPlan],
    pv_slots: list[bool],
    solar_15min: list[float],
    cfg: BatteryConfig,
) -> None:
    """Recompute soc_after for slots affected by PV sell decisions.

    The optimizer builds soc_after assuming all solar charges the battery.
    When _plan_pv_sell_slots() marks a slot as pv_sell=True (PV off), that
    slot's solar energy goes to the grid instead.  The SoC trajectory must
    be corrected forward from the first sell slot so the dashboard chart
    doesn't show phantom battery charging during sell windows.

    Mutates SlotPlan.soc_after in-place.
    """
    soc_max_kwh = cfg.capacity_kwh * cfg.soc_max / 100.0
    soc_min_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
    cap = cfg.capacity_kwh

    # Find the first sell slot — no work needed before it.
    sell_indices = [sp.index for sp in slots if not pv_slots[sp.index]]
    if not sell_indices:
        return
    first_sell = min(sell_indices)

    # Build a mutable SoC map from the existing (pre-correction) values.
    # We'll do a forward pass from the slot before the first sell slot.
    slot_list = sorted(slots, key=lambda sp: sp.index)
    soc_map: dict[int, float] = {sp.index: sp.soc_after * cap / 100.0 for sp in slot_list}

    # Find the SoC entering the first sell slot (= soc_after of the previous slot).
    prev_idx = first_sell - 1
    if prev_idx in soc_map:
        soc = soc_map[prev_idx]
    else:
        # first_sell is the very first slot — use the raw value before correction
        # (soc_after already correct since no prior adjustments).
        # Recover the entering SoC by reversing the original slot's contribution.
        first_sp = next(sp for sp in slot_list if sp.index == first_sell)
        solar_kwh = min(solar_15min[first_sell], cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
        soc = first_sp.soc_after * cap / 100.0 - solar_kwh  # approximate entering SoC

    # Forward pass: recompute soc_after for every slot from first_sell onward.
    slot_map: dict[int, SlotPlan] = {sp.index: sp for sp in slot_list}
    for sp in slot_list:
        s = sp.index
        if s < first_sell:
            soc = sp.soc_after * cap / 100.0
            continue

        selling = not pv_slots[s]
        solar_kw = solar_15min[s] if s < len(solar_15min) else 0.0
        charge_kw = min(solar_kw, cfg.max_charge_kw)
        solar_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency

        if sp.action == "charge":
            # Grid charge: solar contribution doesn't change (charge slot not overridden).
            # Re-apply the grid charge + any solar.
            grid_charge_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
            soc = min(soc + grid_charge_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh)
        elif sp.action == "discharge":
            net_load = cfg.base_load_kw - solar_kw
            if net_load > 0:
                load_kwh = min(net_load, cfg.max_discharge_kw) * SLOT_DURATION_HOURS
            else:
                # Solar surplus during discharge: if selling, no solar to battery.
                load_kwh = 0.0
                if not selling:
                    surplus_kwh = min(-net_load, cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    soc = max(min(soc + surplus_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh), soc_min_kwh)
            # Battery internal draw = delivered kWh / η_d (inverter efficiency loss)
            soc = max(soc - load_kwh / cfg.discharge_efficiency - cfg.idle_drain_per_slot_kwh, soc_min_kwh)
        else:
            # idle / none
            if not selling and solar_kw > cfg.base_load_kw:
                net_kwh = solar_kwh - cfg.idle_drain_per_slot_kwh
                soc = max(min(soc + net_kwh, soc_max_kwh), soc_min_kwh)
            else:
                soc = max(soc - cfg.idle_drain_per_slot_kwh, soc_min_kwh)

        # Patch the SlotPlan in-place.
        sp.soc_after = round(soc / cap * 100.0, 1)


def optimize(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    *,
    start_slot: int = 0,
    initial_soc_pct: float | None = None,
    enable_pv_strategy: bool = False,
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

    # Forward idle-only SoC simulation — determines the peak SoC the battery
    # will reach by letting solar charge it without active discharge.
    # A second parallel simulation starts from soc_min instead of current_soc
    # to compute the two-cycle discharge budget.
    #
    # Two-cycle budget rationale:
    #   When the battery has stored energy AND solar will refill it, we can
    #   discharge in TWO cycles: (1) drain pre-solar overnight from current_soc
    #   down to soc_min, (2) solar recharges with extra headroom because it
    #   starts from soc_min instead of current_soc, (3) drain again post-solar.
    #   Example: current_soc=60%, soc_min=20%, solar fills to 100%.
    #   Single-cycle budget: 100-20 = 80% = 12 kWh.
    #   Two-cycle budget: (60-20)% + (100-20)% = 40+80 = 120% = 18 kWh.
    #   The 6 kWh discharged overnight frees 6 kWh of additional solar headroom,
    #   which is itself dischargeable during the next peak window.
    #   Without this fix, overnight slots with buy_price >> wear_cost are skipped
    #   because the budget is fully consumed by the (cheaper) peak daytime slots.
    _soc_fwd = current_soc_kwh
    peak_soc_kwh = current_soc_kwh
    _soc_fwd2 = soc_min_kwh          # second sim: battery starts empty
    peak_soc_from_min_kwh = soc_min_kwh
    for s in range(start_slot, n):
        if net_loads[s] < 0:
            charge_kw = min(-net_loads[s], cfg.max_charge_kw)
            charge_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
            _soc_fwd = min(_soc_fwd + charge_kwh - idle_drain, soc_max_kwh)
            _soc_fwd2 = min(_soc_fwd2 + charge_kwh - idle_drain, soc_max_kwh)
        else:
            _soc_fwd = _soc_fwd - idle_drain
            _soc_fwd2 = _soc_fwd2 - idle_drain
        _soc_fwd = max(_soc_fwd, soc_min_kwh)
        _soc_fwd2 = max(_soc_fwd2, soc_min_kwh)
        if _soc_fwd > peak_soc_kwh:
            peak_soc_kwh = _soc_fwd
        if _soc_fwd2 > peak_soc_from_min_kwh:
            peak_soc_from_min_kwh = _soc_fwd2

    # Two-cycle discharge budget:
    # — initial_usable: energy in battery right now, dischargeable before solar.
    # — post_solar_usable: peak SoC reachable from solar when starting at soc_min
    #   (models the case where all pre-solar energy was discharged first, giving
    #   maximum solar absorption headroom).
    # Without solar (winter), post_solar_usable = 0 and budget = initial_usable,
    # identical to the old single-cycle formula.
    initial_usable_kwh = max(current_soc_kwh - soc_min_kwh, 0.0)
    post_solar_usable_kwh = max(peak_soc_from_min_kwh - soc_min_kwh, 0.0)
    total_discharge_budget = initial_usable_kwh + post_solar_usable_kwh

    # Find the first slot with meaningful solar production.  Used to split the
    # discharge budget into two independent pools when solar can fully refill
    # the battery (see allocation section below).
    _SOLAR_FLOOR_KW = 0.05
    first_solar_slot = n  # default: no solar today
    for _s in range(start_slot, n):
        if solar_15min[_s] > _SOLAR_FLOOR_KW:
            first_solar_slot = _s
            break

    # When solar can fully recharge the battery from empty, overnight discharge
    # has zero net energy cost — solar replaces it for free.  In this case the
    # pre-solar (overnight) and post-solar (daytime) discharge budgets should
    # be independent pools so that high-price daytime slots do NOT crowd out
    # lower-price overnight slots.
    # Threshold: solar raises battery to ≥ 95 % of soc_max from an empty start.
    solar_full_recharge = post_solar_usable_kwh >= (soc_max_kwh - soc_min_kwh) * 0.95

    # Find profitable discharge and charge slots.
    #
    # Two distinct cases:
    # A) Energy available (existing + solar − idle drain) — discharge at any
    #    slot where the avoided grid buy price exceeds the wear cost.
    # B) Round-trip trades (buy low → discharge later to avoid expensive
    #    buy) — only worthwhile when the price spread covers round-trip
    #    losses + wear.
    profitable_charge: list[int] = []
    profitable_discharge: list[int] = []

    # Case A: discharge when total available energy (current usable + expected
    # solar − idle drain) is positive.  Battery charges from solar excess in
    # idle and discharge states, so SoC being below soc_min at optimisation
    # time does not preclude profitable evening discharge.
    if total_discharge_budget > 0 and discharge_candidates:
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
    #
    # (peak_soc_kwh computed above from forward idle-only SoC simulation)

    plan_actions: dict[int, str] = {}

    # Discharge allocation — two modes depending on whether solar fully refills:
    #
    # SPLIT MODE (solar_full_recharge=True): two independent budget pools.
    #   Pre-solar slots  → budget = initial_usable_kwh  (stored energy now)
    #   Post-solar slots → budget = post_solar_usable_kwh (solar-recharged energy)
    #   Overnight slots (buy ≫ wear_cost) are no longer crowded out by
    #   higher-priced daytime slots that consume from the same shared pool.
    #
    # COMBINED MODE (no/partial solar): single pool = total_discharge_budget,
    #   identical to the previous behaviour — optimal for winter/cloudy days.
    if solar_full_recharge:
        pre_sol_dis = [s for s in profitable_discharge if s < first_solar_slot]
        post_sol_dis = [s for s in profitable_discharge if s >= first_solar_slot]
        pre_budget = initial_usable_kwh
        post_budget = post_solar_usable_kwh
        if profitable_charge:
            post_budget += max(0.0, soc_max_kwh - peak_soc_kwh)

        _LOGGER.debug(
            "SPLIT MODE: first_solar_slot=%d initial_usable=%.2f post_solar_usable=%.2f "
            "pre_sol_dis=%d post_sol_dis=%d pre_budget=%.2f post_budget=%.2f",
            first_solar_slot, initial_usable_kwh, post_solar_usable_kwh,
            len(pre_sol_dis), len(post_sol_dis), pre_budget, post_budget,
        )

        for s in pre_sol_dis:
            if net_loads[s] > 0:
                load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
            else:
                load_kwh = cfg.base_load_kw * SLOT_DURATION_HOURS
            if load_kwh <= 0:
                continue
            battery_kwh = load_kwh / cfg.discharge_efficiency  # internal kWh drawn from cells
            if pre_budget >= battery_kwh:
                plan_actions[s] = "discharge"
                pre_budget -= battery_kwh
            else:
                break

        for s in post_sol_dis:
            if net_loads[s] > 0:
                load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
            else:
                load_kwh = cfg.base_load_kw * SLOT_DURATION_HOURS
            if load_kwh <= 0:
                continue
            battery_kwh = load_kwh / cfg.discharge_efficiency  # internal kWh drawn from cells
            if post_budget >= battery_kwh:
                plan_actions[s] = "discharge"
                post_budget -= battery_kwh
            else:
                break
    else:
        # Combined budget: no full solar recharge, no split needed.
        discharge_energy_available = total_discharge_budget
        if profitable_charge:
            discharge_energy_available += max(0.0, soc_max_kwh - peak_soc_kwh)

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
            battery_kwh = load_kwh / cfg.discharge_efficiency  # internal kWh drawn from cells
            if discharge_energy_available >= battery_kwh:
                plan_actions[s] = "discharge"
                discharge_energy_available -= battery_kwh
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
    # Battery-internal kWh consumed per discharge slot = delivered_kwh / η_d.
    # solar_actual and existing_usable are also battery-internal kWh, so all
    # terms in the energy balance are in the same unit.
    total_discharge_battery_kwh = sum(
        min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS / cfg.discharge_efficiency
        for s, a in plan_actions.items() if a == "discharge"
    )
    solar_actual = available_soc - current_soc_kwh
    existing_usable = current_soc_kwh - soc_min_kwh
    grid_charge_needed = max(0.0, total_discharge_battery_kwh - solar_actual - existing_usable)

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
                # Battery internal draw = delivered kWh / η_d (inverter efficiency loss)
                soc = max(soc - load_kwh / cfg.discharge_efficiency - idle_drain, soc_min_kwh)
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

    if enable_pv_strategy:
        result.thirdparty_pv_slots = _plan_pv_sell_slots(
            cfg, result_slots, solar_15min, buy_prices, sell_prices,
            start_slot=start_slot,
        )
        _correct_soc_for_pv_sells(
            result_slots, result.thirdparty_pv_slots, solar_15min, cfg
        )

    _LOGGER.info(
        "Optimization complete: profit=%.4f€, charge=%d, discharge=%d, "
        "idle=%d slots",
        total_profit, n_charge, n_discharge, n_idle,
    )

    return result
