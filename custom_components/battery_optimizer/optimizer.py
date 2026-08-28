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

# Deepest un-fixable floor violation seen in the current episode.  Module
# state survives across optimizer runs so repeated runs don't re-log the
# same condition; only a deeper dip re-logs.  Reset when a run finds the
# day clean, so the next episode logs fresh.
_soc_floor_warn_depth: float = 0.0


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
    pv_sell_margin: float = 1.0  # multiplier on the stored-value sell threshold
    # Confidence factor on forecast solar when crediting discharge-assigned
    # surplus absorption (over-forecast safety — never plan around solar that
    # may not arrive).  1.0 = trust the forecast fully.
    solar_forecast_margin: float = 0.85

    # SoC floor safeguard — keep-alive charging that prevents the battery
    # idle drain from pulling SoC below soc_min on cloudy/no-arbitrage days.
    enable_soc_safeguard: bool = True
    soc_recovery_buffer_pct: float = 5.0  # charge target = soc_min + buffer

    # Plateau-aware overnight drain — when the day starts above the plateau
    # edge (dead stored energy the day plan cannot monetize), discharge the
    # excess overnight to cover the load instead of buying grid power.
    enable_night_drain: bool = True

    @property
    def soc_floor_target_pct(self) -> float:
        """Keep-alive charge target: soc_min plus a small recovery buffer."""
        return min(self.soc_min + self.soc_recovery_buffer_pct, self.soc_max)

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
    export_kwh: float = 0.0  # solar exported to grid this slot (kWh)


@dataclass
class OptimizationResult:
    """Result of an optimization run."""

    slots: list[SlotPlan] = field(default_factory=list)
    total_profit: float = 0.0
    baseline_cost: float = 0.0  # estimated daily cost without battery (€)
    emaldo_cost: float = 0.0   # estimated daily cost if Emaldo plan followed (€)
    emaldo_modes: list[int] = field(default_factory=list)  # Emaldo AI modes per slot
    emaldo_grid_cost: float = 0.0   # grid cost under Emaldo plan, wear excluded (€)
    emaldo_wear_total: float = 0.0  # wear cost of Emaldo-plan cycles (€)
    emaldo_cycled_kwh: float = 0.0  # battery-internal kWh discharged under Emaldo plan
    charge_slots: int = 0
    discharge_slots: int = 0
    idle_slots: int = 0
    reason: str = ""
    # Slots where the SoC floor safeguard inserted a keep-alive charge.
    safeguard_slots: list[int] = field(default_factory=list)
    # Parallel per-slot PV switch plan (F2 — PV sell strategy).
    # True = third-party PV enabled (battery charges from solar).
    # False = third-party PV disabled (solar sells to grid at spot price).
    # Defaults to all-True (no change in behaviour until _plan_pv_sell_slots fills it).
    thirdparty_pv_slots: list[bool] = field(default_factory=lambda: [True] * 96)
    # Battery cycles and wear accounting (F1 — net savings reporting).
    cycled_kwh: float = 0.0        # battery-internal kWh discharged this plan
    wear_cost_total: float = 0.0   # € of wear from those cycles
    net_profit: float = 0.0        # total_profit minus wear_cost_total
    # Cost decomposition (F1 breakdown attributes).
    remaining_slots: int = 0
    # Baseline (no battery): grid import = full base load, export = solar surplus.
    baseline_import_kwh: float = 0.0
    baseline_export_kwh: float = 0.0
    baseline_import_cost: float = 0.0      # gross grid purchase cost (€)
    baseline_export_revenue: float = 0.0   # net export revenue (€)
    baseline_import_energy: float = 0.0
    baseline_import_transfer: float = 0.0
    baseline_import_tax: float = 0.0
    baseline_import_commission: float = 0.0
    baseline_export_energy: float = 0.0
    baseline_export_commission: float = 0.0
    # Optimizer plan grid decomposition.
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    grid_energy: float = 0.0
    grid_transfer: float = 0.0
    grid_tax: float = 0.0
    grid_commission: float = 0.0
    grid_export_energy: float = 0.0
    grid_export_commission: float = 0.0
    # Emaldo plan grid decomposition.
    emaldo_import_kwh: float = 0.0
    emaldo_export_kwh: float = 0.0
    emaldo_import_energy: float = 0.0
    emaldo_import_transfer: float = 0.0
    emaldo_import_tax: float = 0.0
    emaldo_import_commission: float = 0.0
    emaldo_export_energy: float = 0.0
    emaldo_export_commission: float = 0.0
    trace: dict | None = None   # decision trace — populated on main runs only

    @property
    def slot_values(self) -> list[int]:
        """96 emaldo override byte values."""
        return [s.slot_value for s in self.slots]


def optimizer_plan_cost_breakdown(result: OptimizationResult) -> dict[str, float]:
    """Subcosts forming the optimizer plan cost (baseline_cost - net_profit).

    grid_cost + wear_cost == the sensor state value.  grid_cost is the grid
    purchase cost under the plan (baseline minus gross savings); wear_cost is
    battery wear.  Money attrs 4 dp, cycled 3 dp (mirrors the savings sensor).
    """
    return {
        "grid_cost": round(result.baseline_cost - result.total_profit, 4),
        "wear_cost": round(result.wear_cost_total, 4),
        "cycled_kwh": round(result.cycled_kwh, 3),
        "grid_import_kwh": round(result.grid_import_kwh, 3),
        "grid_export_kwh": round(result.grid_export_kwh, 3),
        "grid_energy": round(result.grid_energy, 4),
        "grid_transfer": round(result.grid_transfer, 4),
        "grid_tax": round(result.grid_tax, 4),
        "grid_commission": round(result.grid_commission, 4),
        "grid_export_energy": round(result.grid_export_energy, 4),
        "grid_export_commission": round(result.grid_export_commission, 4),
    }


def emaldo_plan_cost_breakdown(result: OptimizationResult) -> dict[str, float]:
    """Subcosts forming the Emaldo plan cost (grid cost plus wear).

    emaldo_grid_cost + emaldo_wear_cost == the sensor state value
    (``emaldo_cost``).  Money attrs 4 dp, cycled 3 dp.
    """
    return {
        "emaldo_grid_cost": round(result.emaldo_grid_cost, 4),
        "emaldo_wear_cost": round(result.emaldo_wear_total, 4),
        "emaldo_cycled_kwh": round(result.emaldo_cycled_kwh, 3),
        "emaldo_import_kwh": round(result.emaldo_import_kwh, 3),
        "emaldo_export_kwh": round(result.emaldo_export_kwh, 3),
        "emaldo_energy": round(result.emaldo_import_energy, 4),
        "emaldo_transfer": round(result.emaldo_import_transfer, 4),
        "emaldo_tax": round(result.emaldo_import_tax, 4),
        "emaldo_commission": round(result.emaldo_import_commission, 4),
        "emaldo_export_energy": round(result.emaldo_export_energy, 4),
        "emaldo_export_commission": round(result.emaldo_export_commission, 4),
    }


def baseline_cost_breakdown(result: OptimizationResult) -> dict[str, float]:
    """Subcosts forming the baseline cost (no-battery scenario).

    baseline_import_cost - baseline_export_revenue == the sensor state value.
    Import cost = energy + transfer + tax + commission; export revenue =
    export energy minus commission.  Money attrs 4 dp, kWh 3 dp.
    """
    return {
        "import_cost": round(result.baseline_import_cost, 4),
        "export_revenue": round(result.baseline_export_revenue, 4),
        "import_kwh": round(result.baseline_import_kwh, 3),
        "export_kwh": round(result.baseline_export_kwh, 3),
        "import_energy": round(result.baseline_import_energy, 4),
        "import_transfer": round(result.baseline_import_transfer, 4),
        "import_tax": round(result.baseline_import_tax, 4),
        "import_commission": round(result.baseline_import_commission, 4),
        "export_energy": round(result.baseline_export_energy, 4),
        "export_commission": round(result.baseline_export_commission, 4),
        "remaining_slots": result.remaining_slots,
    }


def _decompose_buy(price: float, cfg: BatteryConfig) -> tuple[float, float, float, float]:
    """Decompose buy_price into (energy, transfer, tax, commission) per kWh.

    Finnish household pricing: buy = spot × VAT + transfer + commission.
    Import tax (VAT on energy) is zero when the energy price is negative.
    Sum of components == buy_price.
    """
    spot = (price - cfg.transfer_fee_buy - cfg.sales_commission) / cfg.vat_multiplier
    transfer = cfg.transfer_fee_buy
    tax = max(0.0, spot) * (cfg.vat_multiplier - 1.0)
    energy = price - transfer - tax - cfg.sales_commission
    return (energy, transfer, tax, cfg.sales_commission)


def _decompose_sell(price: float, cfg: BatteryConfig) -> tuple[float, float, float]:
    """Decompose sell_price into (energy, tax, commission) per kWh.

    No export tax for Finnish household customers; sell = spot − commission.
    Export revenue = energy − commission.
    """
    spot = price + cfg.sales_commission
    return (spot, 0.0, cfg.sales_commission)


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


def _simulate_soc_trajectory(
    plan_actions: dict[int, str],
    net_loads: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    *,
    start_slot: int,
    initial_soc_kwh: float,
    charge_targets: dict[int, int] | None = None,
    discharge_targets: dict[int, int] | None = None,
    pv_slots: list[bool] | None = None,
) -> list[float]:
    """Forward-simulate the true (unclamped) SoC trajectory in kWh.

    Unlike the result-building pass, the lower bound is the physical 0 kWh,
    NOT soc_min — so the trajectory reveals slots where idle drain pulls
    the battery below the configured floor.  Used by the SoC safeguard.

    Returns a list of 96 SoC values (kWh) — SoC *after* each slot.
    """
    n = SLOTS_PER_DAY
    soc_max_kwh = cfg.capacity_kwh * cfg.soc_max / 100.0
    floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
    idle_drain = cfg.idle_drain_per_slot_kwh
    soc = initial_soc_kwh
    out: list[float] = []

    for s in range(n):
        if s < start_slot:
            out.append(soc)
            continue
        action = plan_actions.get(s)
        if action == "charge":
            target_kwh = cfg.capacity_kwh * (charge_targets or {}).get(s, int(cfg.soc_max)) / 100.0
            charge_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
            # Physically a "charge to N%" byte is a no-op when the battery
            # is already above N% — it never discharges down to its target.
            # Idle drain still applies.
            soc = max(
                soc - idle_drain,
                min(soc + charge_kwh - idle_drain, target_kwh),
            )
        elif action == "charge_floor":
            # Keep-alive charge: battery charges only up to the floor target
            # ("charge to N%" byte stops at N%).
            add = min(
                cfg.max_charge_per_slot_kwh * cfg.charge_efficiency,
                max(0.0, floor_target_kwh - soc),
            )
            soc = min(soc + add - idle_drain, soc_max_kwh)
        elif action == "discharge":
            if net_loads[s] > 0:
                load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
                floor_pct = (discharge_targets or {}).get(s, int(cfg.soc_min))
                floor_kwh = cfg.capacity_kwh * floor_pct / 100.0
                # The firmware stops discharging at the slot's SoC marker
                # (user floor N for masked slots, else soc_min) — model that.
                battery_draw = min(
                    load_kwh / cfg.discharge_efficiency,
                    max(0.0, soc - floor_kwh),
                )
                soc = soc - battery_draw - idle_drain
            else:
                # Solar surplus during discharge: only charges the battery when
                # PV is enabled.  Sold solar (pv_slots[s] is False) goes to the
                # grid, so it must not raise the battery SoC forecast.
                pv_on = pv_slots[s] if (pv_slots and s < len(pv_slots)) else True
                if pv_on:
                    surplus_kw = min(-net_loads[s], cfg.max_charge_kw)
                    solar_kwh = surplus_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    soc = min(soc + solar_kwh - idle_drain, soc_max_kwh)
                else:
                    soc = soc - idle_drain
        else:
            # idle / none — absorbs solar surplus only when PV is enabled;
            # sold solar goes to the grid, not the battery.
            pv_on = pv_slots[s] if (pv_slots and s < len(pv_slots)) else True
            if pv_on and net_loads[s] < 0:
                charge_kwh = min(-net_loads[s], cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                soc = min(soc + charge_kwh - idle_drain, soc_max_kwh)
            else:
                soc = soc - idle_drain
        soc = max(soc, 0.0)
        out.append(soc)

    return out


def _find_rescue_slot(
    plan_actions: dict[int, str],
    buy_prices: list[float],
    start_slot: int,
    violation_slot: int,
) -> int | None:
    """Pick a committed discharge slot to flip into a floor-charge slot.

    When the safeguard finds no free slot before a violation, the dip usually
    comes from a planned discharge run ending at the floor: idle drain pushes
    the trajectory slightly below soc_min right after the run.  Instead of
    giving up (and logging the episode), flip the cheapest committed discharge
    slot inside the violated window into ``charge_floor`` — that slot re-buys
    what the run discharged, lifting the whole trajectory after it back above
    the floor.  The reserve (A) prevents most of these; the rescue is the
    fallback for committed plans that predate the reserve or exceed it.

    Cost minimisation: the cheapest discharge slot (by buy price, then closest
    to the violation on ties) is sacrificed; the expensive slots still run.
    Returns None when the window contains no discharge slot to sacrifice.
    """
    rescues = [
        s for s in range(start_slot, violation_slot + 1)
        if plan_actions.get(s) == "discharge"
    ]
    if not rescues:
        return None
    return min(rescues, key=lambda s: (round(buy_prices[s], 4), -s))


def _apply_soc_safeguard(
    plan_actions: dict[int, str],
    buy_prices: list[float],
    net_loads: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    *,
    start_slot: int,
    initial_soc_kwh: float,
) -> list[int]:
    """Insert keep-alive charge slots so SoC never falls below soc_min.

    The battery unit's idle consumption (~0.1 kW) drains SoC continuously.
    On cloudy days with no profitable arbitrage, the greedy pass plans zero
    charge slots and the battery self-discharges below soc_min for days.

    This pass is a CONSTRAINT, not a trade: no profitability test is applied,
    but cost is minimised — charging happens at the cheapest available
    buy-price slot before each projected violation, and only up to
    ``soc_floor_target_pct`` (soc_min + small buffer), so the energy bought
    is just a few hundred Wh.  When no free slot exists, the cheapest
    committed discharge slot in the violated window is flipped into a
    floor-charge slot instead (the rescue) — the one trade this pass makes,
    and only the cheapest slot is sacrificed.

    Mutates plan_actions in place (adds "charge_floor" entries).
    Returns the list of inserted slot indices.
    """
    # Module-level dedup state: read AND written here, so it must be
    # declared global — otherwise the assignments below shadow it and the
    # first read raises UnboundLocalError.
    global _soc_floor_warn_depth

    if not cfg.enable_soc_safeguard:
        return []

    soc_min_kwh = cfg.capacity_kwh * cfg.soc_min / 100.0
    # Small tolerance so a trajectory grazing the floor doesn't trigger.
    tolerance_kwh = cfg.capacity_kwh * 0.005

    inserted: list[int] = []
    max_iterations = 24  # hard cap: enough to bridge >2 days of idle drain
    search_from = start_slot  # advanced past violations that cannot be fixed

    for _ in range(max_iterations):
        traj = _simulate_soc_trajectory(
            plan_actions, net_loads, solar_15min, cfg,
            start_slot=start_slot, initial_soc_kwh=initial_soc_kwh,
        )
        violation_slot = next(
            (
                s for s in range(search_from, SLOTS_PER_DAY)
                if traj[s] < soc_min_kwh - tolerance_kwh
            ),
            None,
        )
        if violation_slot is None:
            # Day is clean — forget the previous episode's max depth so a
            # future un-fixable violation logs again instead of being
            # silenced by the stale max.
            _soc_floor_warn_depth = 0.0
            break

        # Charge must happen at or before the violation.  Candidates are
        # slots not already committed to charge/discharge.
        floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
        candidates = [
            s for s in range(start_slot, violation_slot + 1)
            if plan_actions.get(s) not in ("charge", "charge_floor", "discharge")
        ]

        # Deficit filter: only insert a keep-alive charge when the battery
        # is genuinely below the floor target at that slot.  A charge when
        # soc_before already sits at/above the floor target is a no-op: it
        # adds energy but the trajectory above the floor is fine either way,
        # and on a flat day it manufactures uneconomical grid top-ups.
        # Simulate the SoC entering each candidate slot from the trajectory.
        def _soc_before(slot: int) -> float:
            if slot == start_slot:
                return initial_soc_kwh
            return traj[slot - 1]

        deficit_candidates = [
            s for s in candidates if _soc_before(s) < floor_target_kwh
        ]
        if deficit_candidates:
            # Cheapest buy price wins; on (near-)equal prices prefer the slot
            # closest to the violation — charging right before the dip yields
            # maximum idle-drain headroom per top-up, whereas charging early
            # is partially wasted (battery may already be near the floor target).
            cheapest = min(deficit_candidates, key=lambda s: (round(buy_prices[s], 4), -s))
            plan_actions[cheapest] = "charge_floor"
            inserted.append(cheapest)
            continue

        # Rescue re-buy path: flip the cheapest committed discharge slot in
        # the window into a floor-charge slot, but only when that slot starts
        # below the floor target.  A discharge that begins above the floor
        # must not be cancelled to top up a floor that is not violated.
        rescue_slot = next(
            (
                s for s in sorted(
                    range(start_slot, violation_slot + 1),
                    key=lambda s: (round(buy_prices[s], 4), -s),
                )
                if plan_actions.get(s) == "discharge"
                and _soc_before(s) < floor_target_kwh
            ),
            None,
        )
        if rescue_slot is not None:
            plan_actions[rescue_slot] = "charge_floor"
            inserted.append(rescue_slot)
            # No search_from advance: re-simulate so any downstream
            # violations (post-rescue) still get checked.  A deep dip
            # flips multiple discharge slots over successive iterations.
            continue

        # Neither a free deficit slot nor a deficit discharge slot: skip
        # past it and look for later, fixable violations instead of giving
        # up entirely.
        depth_pct = (soc_min_kwh - traj[violation_slot]) / cfg.capacity_kwh * 100.0
        if depth_pct > _soc_floor_warn_depth:
            _soc_floor_warn_depth = depth_pct
            # INFO for routine shallow dips (<3%): expected on cloudy /
            # no-arbitrage days, self-resolves.  WARNING only for deep
            # dips that may warrant attention.
            log = _LOGGER.warning if depth_pct >= 3.0 else _LOGGER.info
            log(
                "SoC safeguard: floor violation at slot %d (%.1f%% below floor) "
                "has no free or rescueable slot for a keep-alive charge — skipping",
                violation_slot, depth_pct,
            )
        search_from = violation_slot + 1
        continue

    if inserted:
        _LOGGER.info(
            "SoC safeguard: inserted %d keep-alive charge slot(s) at %s "
            "(target %.0f%%) to hold SoC above %.0f%%",
            len(inserted), sorted(inserted),
            cfg.soc_floor_target_pct, cfg.soc_min,
        )
    return inserted


def _plan_pv_sell_slots(
    cfg: BatteryConfig,
    slots: list[SlotPlan],
    solar_15min: list[float],
    buy_prices: list[float],
    sell_prices: list[float],
    *,
    start_slot: int = 0,
    initial_soc_kwh: float | None = None,
) -> list[bool]:
    """Plan which solar slots should sell to grid vs charge the battery.

    Strategy: sell expensive morning solar, then let the battery charge to
    100% uninterrupted from a single cutover point onward.

    A single cutover slot T divides the day:
      [start_slot, T)  — PV off: sell to grid (if sell price beats storage)
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

    # Current SoC at the start of the plan window.  Prefer the explicit
    # initial value — the per-slot soc_after is a *post-slot* value and was
    # historically clamped at soc_min, overstating real SoC on low days.
    if initial_soc_kwh is not None:
        current_soc_kwh = initial_soc_kwh
    else:
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
        if solar_15min[s] >= _MIN_SOLAR_KW and (sp is None or sp.action not in ("charge", "charge_floor")):
            net_kw = solar_15min[s] - cfg.base_load_kw
            if net_kw > 0.0:
                charge_kw = min(net_kw, cfg.max_charge_kw)
                charge_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
        remaining_solar[s] = remaining_solar[s + 1] + charge_kwh

    # If total solar (from start_slot onward) cannot fill the battery,
    # selling would only make things worse — skip.
    if remaining_solar[start_slot] < needed_kwh * cfg.pv_sell_solar_margin:
        return pv_slots

    # Floor recovery determines when selling may start: below the SoC floor
    # the first solar must recharge the battery to soc_min + buffer BEFORE
    # any selling (deep-low dwell is the worst case for LFP longevity).
    effective_noon = min(NOON_SLOT, n)
    sell_from = start_slot
    floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
    if current_soc_kwh < floor_target_kwh:
        soc_sim = current_soc_kwh
        recovery_slot: int | None = None
        for s in range(start_slot, effective_noon):
            sp = slot_map.get(s)
            charge_kwh = 0.0
            if solar_15min[s] >= _MIN_SOLAR_KW and (sp is None or sp.action not in ("charge", "charge_floor")):
                net_kw = solar_15min[s] - cfg.base_load_kw
                if net_kw > 0.0:
                    charge_kwh = min(net_kw, cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
            soc_sim = max(soc_sim + charge_kwh - cfg.idle_drain_per_slot_kwh, 0.0)
            if soc_sim >= floor_target_kwh:
                recovery_slot = s + 1
                break
        if recovery_slot is None:
            # Floor cannot be reached before the cutover — keep all solar.
            _LOGGER.info(
                "PV sell strategy: SoC %.1f%% below floor %.1f%% and solar "
                "before cutover cannot recover it — selling skipped",
                current_soc_kwh / cfg.capacity_kwh * 100.0,
                cfg.soc_floor_target_pct,
            )
            return pv_slots
        sell_from = recovery_slot
        _LOGGER.info(
            "PV sell strategy: SoC below floor — solar charges battery to "
            "%.0f%% first, selling starts at slot %d (%02d:%02d)",
            cfg.soc_floor_target_pct, sell_from,
            (sell_from * 15) // 60, (sell_from * 15) % 60,
        )

    # Iterate to the true needed_kwh.  The plan-start SoC understates the
    # battery gap: during the sell window the battery misses surplus-solar
    # absorption and still covers base-load where solar < base.  Simulate
    # the SoC entering the cutover and re-scan T until the pair stabilises.
    needed_kwh = max(0.0, soc_max_kwh - current_soc_kwh)
    if needed_kwh < 0.01:
        return pv_slots

    T_cutover = effective_noon
    for _ in range(6):
        sim_soc = _forward_soc_sim(
            cfg, slots, solar_15min, current_soc_kwh, start_slot, T_cutover,
            selling_since=sell_from,
        )
        need = max(0.0, soc_max_kwh - sim_soc)
        if need < 0.01:
            T_cutover = start_slot
            break
        if remaining_solar[effective_noon] >= need / cfg.pv_sell_solar_margin:
            new_T = effective_noon  # sell everything up to noon
        else:
            # Post-noon solar not enough — scan backward from noon to find the
            # latest T from which cumulative solar covers the true need.
            new_T = start_slot  # fallback: no selling
            for T in range(effective_noon, start_slot - 1, -1):
                if remaining_solar[T] >= need / cfg.pv_sell_solar_margin:
                    new_T = T
                    break
        prev_need = needed_kwh
        needed_kwh = need
        if new_T == T_cutover or abs(need - prev_need) < 0.1:
            break
        T_cutover = new_T

    # No pre-cutover window to sell (e.g. already past noon).
    if T_cutover <= start_slot:
        return pv_slots

    # Final safety: never sell unless the solar after the cutover really
    # covers the (re-simulated) true need — guards forecast-error margin.
    final_sim = _forward_soc_sim(
        cfg, slots, solar_15min, current_soc_kwh, start_slot, T_cutover,
        selling_since=sell_from,
    )
    final_need = max(0.0, soc_max_kwh - final_sim)
    if remaining_solar[T_cutover] < final_need * cfg.pv_sell_solar_margin:
        _LOGGER.info(
            "PV sell strategy: %d sell slots would starve the battery "
            "(need %.2f kWh, only %.2f kWh after cutover) — selling skipped",
            T_cutover - sell_from, final_need, remaining_solar[T_cutover],
        )
        return pv_slots
    needed_kwh = final_need

    # Mark all solar slots in [sell_from, T_cutover) as sell (if profitable).
    # Economic gate: a stored solar kWh displaces the BEST alternative use of
    # that energy — either a future grid BUY (a planned discharge slot) or, on
    # high-solar days when the battery fills from free solar, a future solar
    # EXPORT (the post-cutover solar the battery would otherwise absorb).
    #   - With discharge slots: stored value = min(discharge buy) × round-trip.
    #   - Without discharge:    stored value = min(sell price) over the
    #     post-cutover solar window (the cheapest export we'd displace).
    # Export only when the slot's sell price clears that stored value (AND the
    # config floor).  The old same-slot sell > buy check is dropped: buy always
    # includes VAT + transfer fees, so it can never open on a normal tariff and
    # left the PV-sell strategy dead precisely on the sunny days it targets.
    # pv_sell_min_price_spread remains the absolute floor; pv_sell_margin
    # scales the threshold (e.g. 1.05 requires the export price to clear the
    # stored value by 5%).
    # Note: selling PV directly to grid incurs zero battery wear, so no
    # wear-cost term is applied here (only the round-trip discount above).
    discharge_slots = [sp.index for sp in slots if sp.action == "discharge"]
    if discharge_slots:
        sell_threshold = (
            min(
                (buy_prices[f] for f in discharge_slots if f < len(buy_prices)),
                default=0.0,
            )
            * cfg.round_trip_factor
            * cfg.pv_sell_margin
        )
    else:
        # No grid buy to displace: stored solar only offsets later solar
        # export.  Reference the cheapest remaining sell price in the
        # post-cutover window — selling now beats storing when this slot's
        # sell price exceeds that (× margin), capturing the morning-vs-midday
        # spread instead of never selling.
        later_sell = [
            sell_prices[t]
            for t in range(T_cutover, n)
            if t < len(sell_prices) and solar_15min[t] >= _MIN_SOLAR_KW
        ]
        sell_threshold = min(later_sell) * cfg.pv_sell_margin if later_sell else None
    for s in range(sell_from, T_cutover):
        sp = slot_map.get(s)
        if solar_15min[s] < _MIN_SOLAR_KW:
            continue
        if sp is not None and sp.action in ("charge", "charge_floor"):
            continue
        if sell_prices[s] <= cfg.pv_sell_min_price_spread:
            continue
        if sell_threshold is not None:
            above_threshold = sell_prices[s] > sell_threshold
        else:
            above_threshold = sell_prices[s] > buy_prices[s]
        if above_threshold:
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
    floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
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
        elif sp.action == "charge_floor":
            # Keep-alive charge: only fills up to the floor target.
            add_kwh = min(
                cfg.max_charge_per_slot_kwh * cfg.charge_efficiency,
                max(0.0, floor_target_kwh - soc),
            )
            soc = min(soc + add_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh)
        elif sp.action == "discharge":
            net_load = cfg.base_load_kw - solar_kw
            if net_load > 0:
                load_kwh = min(net_load, cfg.max_discharge_kw) * SLOT_DURATION_HOURS
            else:
                # Solar surplus during discharge: if selling, no solar to battery.
                load_kwh = 0.0
                if not selling:
                    surplus_kwh = min(-net_load, cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    soc = max(min(soc + surplus_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh), 0.0)
            # Battery internal draw = delivered kWh / η_d, capped at the floor
            # marker (the discharge byte stops at soc_min).
            battery_draw = min(
                load_kwh / cfg.discharge_efficiency,
                max(0.0, soc - soc_min_kwh),
            )
            soc = max(soc - battery_draw - cfg.idle_drain_per_slot_kwh, 0.0)
        else:
            # idle / none
            if not selling and solar_kw > cfg.base_load_kw:
                net_kwh = solar_kwh - cfg.idle_drain_per_slot_kwh
                soc = max(min(soc + net_kwh, soc_max_kwh), 0.0)
            else:
                soc = max(soc - cfg.idle_drain_per_slot_kwh, 0.0)

        # Patch the SlotPlan in-place.
        sp.soc_after = round(soc / cap * 100.0, 1)


def _forward_soc_sim(
    cfg: BatteryConfig,
    slots: list[SlotPlan],
    solar_15min: list[float],
    soc_start_kwh: float,
    start_slot: int,
    end_slot: int,
    selling_since: int,
) -> float:
    """Simulate SoC (kWh) through [start_slot, end_slot) under the PV-sell plan.

    Selling is active from selling_since onward: solar goes to grid, the
    battery neither charges from surplus nor absorbs it.  Plan actions are
    fixed (independent of the sell window), so the result is deterministic.
    Energy accounting mirrors _correct_soc_for_pv_sells.
    """
    cap = cfg.capacity_kwh
    soc_max_kwh = cap * cfg.soc_max / 100.0
    soc_min_kwh = cap * cfg.soc_min / 100.0
    floor_target_kwh = cap * cfg.soc_floor_target_pct / 100.0
    slot_map = {sp.index: sp for sp in slots}
    soc = soc_start_kwh
    for s in range(start_slot, end_slot):
        sp = slot_map.get(s)
        selling = s >= selling_since
        solar_kw = solar_15min[s] if s < len(solar_15min) else 0.0
        charge_kw = min(solar_kw, cfg.max_charge_kw)
        solar_kwh = charge_kw * SLOT_DURATION_HOURS * cfg.charge_efficiency
        if sp is None:
            soc = max(soc - cfg.idle_drain_per_slot_kwh, 0.0)
            continue
        if sp.action == "charge":
            grid_charge_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
            soc = min(soc + grid_charge_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh)
        elif sp.action == "charge_floor":
            add_kwh = min(
                cfg.max_charge_per_slot_kwh * cfg.charge_efficiency,
                max(0.0, floor_target_kwh - soc),
            )
            soc = min(soc + add_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh)
        elif sp.action == "discharge":
            net_load = cfg.base_load_kw - solar_kw
            if net_load > 0:
                load_kwh = min(net_load, cfg.max_discharge_kw) * SLOT_DURATION_HOURS
            else:
                # Solar surplus during discharge: battery firmware absorbs it
                # unless selling (safety-net semantics, see _correct_soc_for_pv_sells).
                load_kwh = 0.0
                if not selling:
                    surplus_kwh = min(-net_load, cfg.max_charge_kw) * SLOT_DURATION_HOURS * cfg.charge_efficiency
                    soc = max(min(soc + surplus_kwh - cfg.idle_drain_per_slot_kwh, soc_max_kwh), 0.0)
            battery_draw = min(
                load_kwh / cfg.discharge_efficiency,
                max(0.0, soc - soc_min_kwh),
            )
            soc = max(soc - battery_draw - cfg.idle_drain_per_slot_kwh, 0.0)
        else:
            # idle / none
            if not selling and solar_kw > cfg.base_load_kw:
                net_kwh = solar_kwh - cfg.idle_drain_per_slot_kwh
                soc = max(min(soc + net_kwh, soc_max_kwh), 0.0)
            else:
                soc = max(soc - cfg.idle_drain_per_slot_kwh, 0.0)
    return soc


# ---------------------------------------------------------------------------
# Plateau-aware overnight drain (probe machinery)
#
# COMBINED-mode day: the single discharge pool covers evening -> morning ->
# night LAST, so the night slots (cheapest avoided buys) starve.  The day's
# profit is FLAT over a range of starting SoCs (the plateau) — the excess
# stored energy above the plateau edge is dead.  Probes measure the day
# profit as a function of the starting SoC; the edge is where it drops.
# The overnight drain discharges the dead excess (down to the edge) during
# the pre-solar night window, displacing grid buys.
# ---------------------------------------------------------------------------

_EDGE_EPSILON_EUR = 0.005   # plateau match tolerance (€) — profit noise floor
_EDGE_MARGIN_PP = 5.0       # edge never below floor target + margin (pp)
_NIGHT_DRAIN_MIN_PP = 1.0   # minimum excess to trigger a night drain (pp)

# Night-reserve bias — COMBINED-mode tuning constant.
#
# The combined discharge pool is consumed buy-desc across ALL profitable
# slots, so the cheapest pre-solar night slots starve while the battery
# parks above the plateau edge and exports surplus solar in the afternoon.
# The reservation (see discharge allocation below) sets aside a share of the
# initial stored energy for pre-solar night slots, scaled by how much of the
# usable band solar refills (refill_fraction / 0.95).
#
# Tuning: 0.0 = disabled (legacy combined behavior); 1.0 = full reservation
# (all initial energy to the night pool).  Higher drains deeper overnight:
# more cheap-night savings, lower daytime min SoC, forced afternoon export
# postponed or eliminated.  Live partial-solar day (refill 0.8175):
#   0.0 -> baseline (6 night slots, min 52.4 %, export at 17:00)
#   0.6 -> +0.46 EUR/day, 20 night slots, min 27.7 %, no forced export  <- default
#   0.8 -> +0.61 EUR/day, 24 night slots, min 20.7 % (0.7 pp above floor)
#   0.889+ -> plateau (+0.61 EUR/day, min ~21 %) — no further gain.
_NIGHT_RESERVE_BIAS = 0.6


def _probe_result(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    start_slot: int,
    initial_soc_pct: float,
    enable_pv_strategy: bool,
    emaldo_modes: list[int] | None,
    case_a_floor: float,
) -> OptimizationResult:
    """One full optimize() run with the night drain disabled (a "probe").

    _probe=True prevents recursion — probes never re-probe (depth <= 2).
    ``case_a_floor`` is the main run's Case A discharge floor — the probe must
    measure the same profit curve the real plan will use.
    """
    return optimize(
        buy_prices, sell_prices, solar_15min, cfg,
        start_slot=start_slot, initial_soc_pct=initial_soc_pct,
        enable_pv_strategy=enable_pv_strategy, emaldo_modes=emaldo_modes,
        enable_night_drain=False, _probe=True, case_a_floor=case_a_floor,
    )


def _find_plateau_edge(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    start_slot: int,
    current_soc_pct: float,
    p0_profit: float,
    enable_pv_strategy: bool,
    emaldo_modes: list[int] | None,
    case_a_floor: float,
) -> float:
    """Lowest start SoC whose day profit still matches P0 (within eps).

    Profit is flat on the plateau (dead stored energy) and drops below the
    edge.  Binary search over [lb, current] where lb = floor target + margin,
    so the returned edge never risks the drain-to-floor regression.  Returns
    the plateau edge in percent points (resolution ~ 1 pp).
    """
    lb_pct = min(
        current_soc_pct,
        cfg.soc_floor_target_pct + cfg.soc_recovery_buffer_pct + _EDGE_MARGIN_PP,
    )
    hi_pct = current_soc_pct
    while hi_pct - lb_pct > 1.0:
        mid_pct = (lb_pct + hi_pct) / 2.0
        profit = _probe_result(
            buy_prices, sell_prices, solar_15min, cfg,
            start_slot, mid_pct, enable_pv_strategy, emaldo_modes,
            case_a_floor,
        ).total_profit
        if profit >= p0_profit - _EDGE_EPSILON_EUR:
            hi_pct = mid_pct
        else:
            lb_pct = mid_pct
    return hi_pct


def _build_night_drain_plan(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    start_slot: int,
    first_solar_slot: int,
    edge_pct: float,
    excess_kwh: float,
    enable_pv_strategy: bool,
    emaldo_modes: list[int] | None,
    case_a_floor: float,
) -> dict[int, str] | None:
    """Merged plan = edge-optimal day plan + overnight discharge of excess.

    Night window is [start_slot, first_solar_slot): slots the edge plan does
    not discharge, where load is grid-covered (net_load > 0) and the avoided
    buy beats the wear cost.  Slots sorted buy-desc — the most expensive
    night grid buys are displaced first.  Returns None when no night slot can
    absorb any excess (caller falls back to the plain plan).
    """
    net_loads = [
        cfg.base_load_kw - (solar_15min[s] if s < len(solar_15min) else 0.0)
        for s in range(SLOTS_PER_DAY)
    ]
    edge_result = _probe_result(
        buy_prices, sell_prices, solar_15min, cfg,
        start_slot, edge_pct, enable_pv_strategy, emaldo_modes,
        case_a_floor,
    )
    plan: dict[int, str] = {
        sp.index: sp.action for sp in edge_result.slots if sp.action != "none"
    }
    wear_cost = cfg.wear_cost_per_kwh
    window = [
        s for s in range(start_slot, min(first_solar_slot, SLOTS_PER_DAY))
        if plan.get(s) in (None, "idle")
        and net_loads[s] > 0
        and buy_prices[s] > case_a_floor
    ]
    window.sort(key=lambda s: buy_prices[s], reverse=True)
    remaining = excess_kwh
    marked = 0
    for s in window:
        load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
        if load_kwh <= 0:
            continue
        battery_kwh = load_kwh / cfg.discharge_efficiency  # internal kWh
        if remaining < battery_kwh:
            break
        plan[s] = "discharge"
        remaining -= battery_kwh
        marked += 1
    if marked == 0:
        return None
    return plan


def optimize(
    buy_prices: list[float],
    sell_prices: list[float],
    solar_15min: list[float],
    cfg: BatteryConfig,
    *,
    start_slot: int = 0,
    initial_soc_pct: float | None = None,
    enable_pv_strategy: bool = False,
    emaldo_modes: list[int] | None = None,
    enable_night_drain: bool = True,
    _probe: bool = False,
    solar_regime_engaged: bool = False,
    future_min_buy: float | None = None,
    case_a_floor: float | None = None,
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
    night_drain_plan: dict[int, str] | None = None  # bound in the drain guard below

    if initial_soc_pct is not None:
        current_soc_kwh = cfg.capacity_kwh * initial_soc_pct / 100.0
    else:
        # Issue #16 hardening: the coordinator refuses to call with None, so
        # this only fires for direct API callers.  Plan quality is degraded
        # (zero discharge budget) — log it as an error, not a warning.
        _LOGGER.error(
            "initial_soc_pct is None — defaulting to soc_min (%.0f%%). "
            "Schedule will assume near-empty battery and DISCHARGE NOTHING!",
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

    # Per-slot fee decomposition (F1 breakdown attributes).  Buy prices
    # decompose into (energy, transfer, tax, commission); sell prices into
    # (energy, tax=0, commission).  Precomputed once per run.
    buy_decomp = [_decompose_buy(p, cfg) for p in buy_prices]
    sell_decomp = [_decompose_sell(p, cfg) for p in sell_prices]

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
    #
    # Discharge reserve (A): the dischargeable bottom edge is soc_min PLUS the
    # recovery buffer, not soc_min itself.  A planned run ending exactly at
    # soc_min left the battery no headroom for idle drain — the sauna-night
    # episode drained to the floor and idle drain pulled tomorrow's plan
    # 3.3-4.0 % below it.  The reserve makes the last discharge slot stop at
    # soc_min + buffer; the buffer then absorbs overnight idle drain before the
    # floor is touched.
    discharge_reserve_kwh = cfg.capacity_kwh * cfg.soc_recovery_buffer_pct / 100.0
    initial_usable_kwh = max(current_soc_kwh - soc_min_kwh - discharge_reserve_kwh, 0.0)
    post_solar_usable_kwh = max(peak_soc_from_min_kwh - soc_min_kwh - discharge_reserve_kwh, 0.0)
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
    # Threshold: solar raises battery to >= 95 % of the usable band from the
    # floor.  Compare peak SoC (from floor) against the band top, NOT the
    # post-reserve usable kWh - the old form subtracted the discharge reserve
    # from the left side only, making the gate impossible (needs > capacity).
    refill_fraction = max(0.0, (peak_soc_from_min_kwh - soc_min_kwh) / (soc_max_kwh - soc_min_kwh))
    solar_full_recharge = refill_fraction >= 0.95

    # Night reserve (COMBINED mode only — SPLIT mode below already gives the
    # pre-solar slots their own pool, so the reservation is a no-op there):
    # set aside a bias-scaled share of the initial stored energy for the
    # pre-solar night slots.  The combined greedy pool allocates buy-desc
    # across every profitable slot, so the cheapest overnight avoidances
    # starve while the battery sits above the plateau edge and exports
    # surplus solar in the afternoon.  Reserving the night share first fixes
    # that (see the allocation section).  Scaled by refill_fraction so a
    # near-full-recharge day reserves nearly the whole bias share while a
    # winter day with tiny solar reserves almost nothing (no solar means no
    # daytime refill to absorb the deeper drain — the plateau probe handles
    # that case).
    night_reserve_kwh = (
        initial_usable_kwh * min(1.0, _NIGHT_RESERVE_BIAS * refill_fraction / 0.95)
        if refill_fraction > 0 else 0.0
    )

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
    #
    # Durable no-refill regime (solar_regime.py engaged): stored kWh can only
    # be replaced by a future GRID purchase, so each kWh discharged must beat
    # the cheapest known future recharge through the round-trip (η²) plus
    # wear.  cheapest_future = min(remaining today, tomorrow when known).
    # When today's solar can still fully refill the battery
    # (solar_full_recharge) or the regime is off, the plain wear floor
    # applies — free solar refill makes stored energy worth burning at any
    # price above wear.  The computed floor is threaded into the night-drain
    # probes so the plateau measurement matches the real plan.
    if case_a_floor is None:
        if solar_regime_engaged and not solar_full_recharge:
            today_remaining = buy_prices[start_slot:] or buy_prices
            today_min = min(today_remaining)
            if future_min_buy is not None:
                cheapest_future = min(today_min, future_min_buy)
            else:
                cheapest_future = today_min
            case_a_floor = cheapest_future / cfg.round_trip_factor + wear_cost
        else:
            case_a_floor = wear_cost

    if total_discharge_budget > 0 and discharge_candidates:
        for s in discharge_candidates:
            if buy_prices[s] > case_a_floor:
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

    # Phase 0 — plateau-aware overnight drain (COMBINED mode only).
    #
    # On no/partial-solar days the combined discharge pool covers evening ->
    # morning -> night LAST, so night slots starve.  Probe the day profit as a
    # function of the start SoC (P0 = current).  The plateau edge is the lowest
    # start SoC with the same profit — everything above it is dead stored
    # energy.  Discharging it overnight (down to the edge) displaces the
    # most-expensive night grid buys without touching the day plan.
    #
    # Guards: only in COMBINED mode (no surplus solar to absorb the drain),
    # only when the day already starts above the floor, never inside a probe,
    # and only when the plan window still contains the night (start_slot must
    # precede the first solar slot).
    if (
        enable_night_drain
        and not _probe
        and not solar_full_recharge
        and initial_soc_pct is not None
        and start_slot < first_solar_slot
        and total_discharge_budget > 0
    ):
        if initial_soc_pct - cfg.soc_floor_target_pct >= _NIGHT_DRAIN_MIN_PP:
            # P0 = plain day profit at the current SoC (measured by a probe;
            # the plain plan's own total_profit is only computed later).
            p0_profit = _probe_result(
                buy_prices, sell_prices, solar_15min, cfg,
                start_slot, initial_soc_pct,
                enable_pv_strategy, emaldo_modes,
                case_a_floor,
            ).total_profit
            edge_pct = _find_plateau_edge(
                buy_prices, sell_prices, solar_15min, cfg,
                start_slot, initial_soc_pct, p0_profit,
                enable_pv_strategy, emaldo_modes,
                case_a_floor,
            )
            if edge_pct < initial_soc_pct:
                excess_kwh = (
                    (initial_soc_pct - edge_pct) / 100.0
                ) * cfg.capacity_kwh
                night_drain_plan = _build_night_drain_plan(
                    buy_prices, sell_prices, solar_15min, cfg,
                    start_slot, first_solar_slot, edge_pct,
                    excess_kwh, enable_pv_strategy, emaldo_modes,
                    case_a_floor,
                )
                if night_drain_plan is not None:
                    # Phase 2 — the merged plan overrides the plain one.  Any
                    # night slot we assigned was idle/none in the edge plan, so
                    # the subsequent grid-charging / idle passes skip it.
                    plan_actions = night_drain_plan

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
        # Night reservation (COMBINED mode): pre-allocate the reserved pool
        # to pre-solar night slots, cheapest buy first.  Night slots are REAL
        # grid-buy avoidances (load > 0 overnight), while many post-solar
        # discharge slots are solar-surplus safety-nets whose effective
        # value is far below their buy price.  Without the reservation the
        # shared combined pool is consumed by the higher-priced day slots and
        # cheap night slots starve — battery parks above the plateau edge and
        # exports surplus solar in the afternoon.  The combined loop below
        # skips slots assigned here and allocates the remaining budget
        # buy-desc across the day as before (bias = 0 -> no reservation ->
        # byte-identical legacy behavior).
        if night_reserve_kwh > 0 and start_slot < first_solar_slot:
            night_energy_available = night_reserve_kwh
            # Cheapest night slots first (ascending buy) — they are the ones
            # the combined pool starves; the expensive night slots the combined
            # loop would pick anyway stay in the shared pool.
            for s in sorted(
                (s for s in profitable_discharge if s < first_solar_slot),
                key=lambda s: (round(buy_prices[s], 4), -s),
            ):
                if s in plan_actions:
                    continue
                if net_loads[s] > 0:
                    # Grid slot — discharge covers household load from battery
                    load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
                else:
                    # Solar surplus slot — reserve only what the firmware
                    # would actually draw: full base load as the worst case
                    # (no solar at all) matches the combined loop's estimate.
                    load_kwh = cfg.base_load_kw * SLOT_DURATION_HOURS
                if load_kwh <= 0:
                    continue
                battery_kwh = load_kwh / cfg.discharge_efficiency  # internal kWh drawn from cells
                if night_energy_available >= battery_kwh:
                    plan_actions[s] = "discharge"
                    night_energy_available -= battery_kwh
                else:
                    break  # Reserve exhausted — remaining night slots stay unassigned

        # Combined budget: no full solar recharge, no split needed.  The
        # night reservation above committed its share of the initial pool;
        # the over-commit correction below is the floor safety net — it
        # drops the cheapest committed discharge slot when the true
        # trajectory violates the floor target (reserved slots are the
        # cheapest committed, so a correction trims the reservation tail
        # first, same as the validated design).
        discharge_energy_available = total_discharge_budget
        if profitable_charge:
            discharge_energy_available += max(0.0, soc_max_kwh - peak_soc_kwh)

        for s in profitable_discharge:
            if s in plan_actions:
                continue  # Assigned by the night reservation above
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

    # Discharge-assigned surplus slots also absorb solar — the Emaldo
    # firmware charges from excess solar even in discharge mode.  The idle
    # loop above skipped them (already assigned), which understated solar
    # absorption in grid_charge_needed below.  Credit it here, scaled by
    # the solar forecast margin so an over-forecast never leaves the plan
    # grid-charge-starved.
    for s in solar_surplus_slots:
        if plan_actions.get(s) != "discharge":
            continue
        surplus_kw = -net_loads[s]
        charge_kwh = (
            min(surplus_kw, cfg.max_charge_kw)
            * SLOT_DURATION_HOURS
            * cfg.charge_efficiency
        )
        charge_kwh = min(charge_kwh, max(0.0, soc_max_kwh - available_soc))
        charge_kwh *= cfg.solar_forecast_margin
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
    # Same discharge reserve as the greedy budget (A): grid charging covers
    # the deficit that solar + existing SoC cannot, where "existing SoC"
    # stops at soc_min + recovery buffer so the charged plan also ends its
    # discharge runs at the reserve line, not at the floor.
    existing_usable = max(
        current_soc_kwh - soc_min_kwh - discharge_reserve_kwh, 0.0
    )
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

    # Discharge over-commit correction: the two-cycle budget double-counts
    # the battery — initial_usable + post_solar_usable can exceed physical
    # capacity above the floor, so an at-budget plan drains below
    # soc_min + reserve.  Forward-sim the true trajectory and drop the
    # cheapest committed discharge slot until the min SoC holds the floor
    # target.  Re-simulate after each drop.  Runs BEFORE the safeguard so
    # the safeguard never sees a plan that violates the floor (no keep-alive
    # top-up on top of a fix).
    floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
    _guard = 0
    while _guard < SLOTS_PER_DAY:
        dis_slots_a = sorted(
            (s for s, a in plan_actions.items() if a == "discharge"),
            key=lambda s: (round(buy_prices[s], 4), -s),
        )
        if not dis_slots_a:
            break
        traj_a = _simulate_soc_trajectory(
            plan_actions, net_loads, solar_15min, cfg,
            start_slot=start_slot, initial_soc_kwh=current_soc_kwh,
        )
        # The floor check applies to the discharge-affected region only,
        # from the first discharge slot onward.  A dip before that point is
        # NOT caused by the plan's discharge — the battery can be parked
        # at/below the floor target at plan time (e.g. soc_min in the
        # morning), and solar refills it before the first discharge runs.
        # Checking min() over the whole trajectory there makes the loop
        # unable to ever satisfy the floor (the start dip is permanent, and
        # dropping discharge does not raise it), so it drops EVERY discharge
        # slot and empties the plan.  The trough of the discharge run is the
        # over-commit this correction exists to trim.
        first_dis = min(s for s, a in plan_actions.items() if a == "discharge")
        if min(traj_a[first_dis:]) >= floor_target_kwh:
            break
        _drop = dis_slots_a[0]
        del plan_actions[_drop]
        _guard += 1

    if _guard:
        _LOGGER.debug(
            "Over-commit correction: dropped %d discharge slot(s) to hold "
            "floor %.1f%% (double-counted budget)",
            _guard, cfg.soc_floor_target_pct,
        )

    # Step 3b: SoC floor safeguard — keep-alive charging.
    # The greedy pass only charges when arbitrage is profitable.  On cloudy,
    # flat-price days no charge is planned and the unit's idle drain pulls
    # SoC below soc_min.  Insert minimal-cost "charge to floor" slots at the
    # cheapest prices so the projected SoC never violates the floor.
    safeguard_slots = _apply_soc_safeguard(
        plan_actions, buy_prices, net_loads, solar_15min, cfg,
        start_slot=start_slot, initial_soc_kwh=current_soc_kwh,
    )

    # Step 4: Build the result
    # NOTE: the SoC trajectory below is intentionally NOT clamped at soc_min.
    # Idle drain genuinely pulls the battery below the configured floor when
    # nothing recharges it — the dashboard forecast must show that reality
    # (the safeguard above prevents it when enabled).  Physical floor is 0.
    floor_target_kwh = cfg.capacity_kwh * cfg.soc_floor_target_pct / 100.0
    floor_charge_byte = _soc_to_charge_target(cfg.soc_floor_target_pct)
    result_slots: list[SlotPlan] = []
    soc = current_soc_kwh
    # Two accumulators: grid cost with battery vs without (baseline).
    # savings = baseline_cost - actual_cost (positive = saved money).
    actual_cost = 0.0
    baseline_cost = 0.0
    n_charge = n_discharge = n_idle = 0
    cycled_kwh = 0.0  # battery-internal kWh discharged (F1 wear accounting)
    # F1 breakdown accumulators.
    baseline_import_kwh = baseline_export_kwh = 0.0
    bl_import_energy = bl_import_transfer = bl_import_tax = bl_import_commission = 0.0
    bl_export_energy = bl_export_commission = 0.0
    grid_import_kwh = grid_export_kwh = 0.0
    g_import_energy = g_import_transfer = g_import_tax = g_import_commission = 0.0
    g_export_energy = g_export_commission = 0.0

    for s in range(n):
        action = "none"
        slot_value = SLOT_NO_OVERRIDE
        profit = 0.0
        actual_grid_kwh = 0.0  # grid electricity bought this slot
        export_kwh = 0.0       # solar exported to grid this slot

        if s < start_slot:
            # Past slots — don't touch
            pass
        elif s in plan_actions:
            action = plan_actions[s]
            if action == "idle" and net_loads[s] < 0:
                # Idle with solar surplus — absorbs solar without grid draw
                slot_value = SLOT_IDLE
                surplus_ac = (-net_loads[s]) * SLOT_DURATION_HOURS
                absorb_ac = min(
                    surplus_ac,
                    cfg.max_charge_per_slot_kwh,
                    (soc_max_kwh - soc) / cfg.charge_efficiency,
                )
                charge_kwh = absorb_ac * cfg.charge_efficiency
                net_kwh = charge_kwh - idle_drain
                soc = max(min(soc + net_kwh, soc_max_kwh), 0.0)
                export_kwh = max(0.0, surplus_ac - absorb_ac)
                n_idle += 1
            elif action == "charge":
                slot_value = charge_target
                # Fix 1+3: compute actual energy from SoC headroom
                max_bat_kwh = cfg.max_charge_per_slot_kwh * cfg.charge_efficiency
                headroom = max(0.0, soc_max_kwh - soc + idle_drain)
                actual_bat_kwh = min(max_bat_kwh, headroom)
                soc = min(soc + actual_bat_kwh - idle_drain, soc_max_kwh)
                actual_grid_kwh = actual_bat_kwh / cfg.charge_efficiency
                actual_grid_kwh += max(net_loads[s], 0) * SLOT_DURATION_HOURS
                n_charge += 1
            elif action == "charge_floor":
                # Keep-alive charge: "charge to N%" byte stops at the floor
                # target, so only the deficit energy is bought from the grid.
                slot_value = floor_charge_byte
                add_kwh = min(
                    cfg.max_charge_per_slot_kwh * cfg.charge_efficiency,
                    max(0.0, floor_target_kwh - soc),
                )
                soc = min(soc + add_kwh - idle_drain, soc_max_kwh)
                # Cost of grid energy bought (before charge losses)
                actual_grid_kwh = add_kwh / cfg.charge_efficiency
                actual_grid_kwh += max(net_loads[s], 0) * SLOT_DURATION_HOURS
                n_charge += 1
            elif action == "discharge":
                if net_loads[s] > 0:
                    load_kwh = min(net_loads[s], cfg.max_discharge_kw) * SLOT_DURATION_HOURS
                else:
                    # Solar surplus slot in discharge mode — firmware will
                    # charge from excess solar.  For SoC accounting, assume
                    # the solar covers load and the net SoC change is small
                    # (solar charge minus idle drain).
                    surplus_ac = (-net_loads[s]) * SLOT_DURATION_HOURS
                    absorb_ac = min(
                        surplus_ac,
                        cfg.max_charge_per_slot_kwh,
                        (soc_max_kwh - soc) / cfg.charge_efficiency,
                    )
                    solar_kwh = absorb_ac * cfg.charge_efficiency
                    soc = max(min(soc + solar_kwh, soc_max_kwh), 0.0)
                    load_kwh = 0.0
                    export_kwh = max(0.0, surplus_ac - absorb_ac)
                # Mirror the Emaldo discharge model so the two cost sensors
                # converge: battery AC output = draw_ac (capped by headroom
                # and max discharge rate), the inverter efficiency loss is
                # metered as grid import (matching the Emaldo device), and SoC
                # drops by the delivered energy.  The discharge byte stops at
                # the floor marker — commanded discharge cannot pull SoC below
                # soc_min; only the idle drain continues past it.
                draw_ac = min(
                    load_kwh,
                    max(0.0, (soc - soc_min_kwh) / cfg.discharge_efficiency),
                    cfg.max_discharge_kw * SLOT_DURATION_HOURS,
                )
                cycled_kwh += draw_ac
                soc = max(soc - draw_ac * cfg.discharge_efficiency - idle_drain, 0.0)
                actual_grid_kwh += max(0.0, load_kwh - draw_ac * cfg.discharge_efficiency)
                # Per-slot SoC threshold: battery discharges only while
                # SoC is above the planned post-slot level.  This protects
                # the schedule against unexpected load spikes — if a large
                # consumer (e.g. sauna) drains the battery faster than
                # planned, it stops at this slot's threshold instead of
                # emptying to soc_min.
                soc_pct_after = soc / cfg.capacity_kwh * 100.0
                slot_value = _soc_to_discharge_target(max(soc_pct_after, cfg.soc_min))
                n_discharge += 1
            else:
                # explicit idle assigned in solar surplus allocation
                slot_value = SLOT_IDLE
                soc = max(soc - idle_drain, 0.0)
                actual_grid_kwh = max(net_loads[s], 0) * SLOT_DURATION_HOURS
                n_idle += 1
        else:
            if s >= start_slot:
                if net_loads[s] < 0:
                    # Solar surplus slot — idle absorbs solar without grid draw
                    action = "idle"
                    slot_value = SLOT_IDLE
                    surplus_ac = (-net_loads[s]) * SLOT_DURATION_HOURS
                    absorb_ac = min(
                        surplus_ac,
                        cfg.max_charge_per_slot_kwh,
                        (soc_max_kwh - soc) / cfg.charge_efficiency,
                    )
                    charge_kwh = absorb_ac * cfg.charge_efficiency
                    net_kwh = charge_kwh - idle_drain
                    soc = max(min(soc + net_kwh, soc_max_kwh), 0.0)
                    export_kwh = max(0.0, surplus_ac - absorb_ac)
                    n_idle += 1
                else:
                    # Idle — explicitly override to idle
                    action = "idle"
                    slot_value = SLOT_IDLE
                    soc = max(soc - idle_drain, 0.0)
                    actual_grid_kwh = max(net_loads[s], 0) * SLOT_DURATION_HOURS
                    n_idle += 1

        # --- Baseline: cost without battery ---
        # No battery: grid imports the FULL base load at buy price; solar
        # surplus above base load is exported at sell price.  Always
        # positive on net (import cost dominates), unlike the old net-load
        # formula which turned negative on sunny days.
        # Skip past slots to match actual_cost / emaldo_cost which also skip them.
        bp = buy_prices[s] if s < len(buy_prices) else 0.0
        sp = sell_prices[s] if s < len(sell_prices) else 0.0
        if s >= start_slot:
            # No battery: solar self-consumes first, so the grid sees only the
            # net deficit (import) and the net surplus (export).  This is the
            # identical treatment to actual_cost / emaldo_cost
            # (bp*import - sp*export), so all three scenarios price solar the
            # same way and the comparison is apples-to-apples.
            # Importing the full base load while ALSO crediting surplus export
            # was physically impossible — a kWh covering the load is neither
            # imported nor sold, and it inflated the baseline by ~€1.20/day.
            net_load = cfg.base_load_kw - solar_15min[s]
            baseline_import = max(0.0, net_load) * SLOT_DURATION_HOURS
            baseline_export = max(0.0, -net_load) * SLOT_DURATION_HOURS
            baseline_slot = bp * baseline_import - sp * baseline_export
            baseline_cost += baseline_slot
            # Decomposed baseline subcosts.
            b_ie, b_it, b_itx, b_ic = buy_decomp[s]
            b_ee, b_etx, b_ec = sell_decomp[s]
            baseline_import_kwh += baseline_import
            baseline_export_kwh += baseline_export
            bl_import_energy += b_ie * baseline_import
            bl_import_transfer += b_it * baseline_import
            bl_import_tax += b_itx * baseline_import
            bl_import_commission += b_ic * baseline_import
            bl_export_energy += b_ee * baseline_export
            bl_export_commission += b_ec * baseline_export
        else:
            baseline_slot = 0.0

        # --- Actual: grid cost with battery (charge cost minus sell revenue) ---
        actual_slot = bp * actual_grid_kwh - sp * export_kwh
        actual_cost += actual_slot
        # Decomposed optimizer subcosts (0 for past slots — accumulators
        # start at 0.0 each iteration).
        g_ie, g_it, g_itx, g_ic = buy_decomp[s]
        g_ee, g_etx, g_ec = sell_decomp[s]
        grid_import_kwh += actual_grid_kwh
        grid_export_kwh += export_kwh
        g_import_energy += g_ie * actual_grid_kwh
        g_import_transfer += g_it * actual_grid_kwh
        g_import_tax += g_itx * actual_grid_kwh
        g_import_commission += g_ic * actual_grid_kwh
        g_export_energy += g_ee * export_kwh
        g_export_commission += g_ec * export_kwh

        # Per-slot profit = savings this slot (positive = saved, negative = spent)
        profit = baseline_slot - actual_slot

        result_slots.append(SlotPlan(
            index=s,
            action=action,
            slot_value=slot_value,
            buy_price=bp,
            sell_price=sp,
            solar_kw=solar_15min[s] if s < len(solar_15min) else 0.0,
            load_kw=cfg.base_load_kw,
            soc_after=soc / cfg.capacity_kwh * 100.0,
            profit=profit,
            export_kwh=export_kwh,
        ))

    # --- Emaldo plan cost: simulate battery following the Emaldo AI modes ---
    # 3rd-party-PV handling matters here: with third-party PV enabled the
    # device charges the battery from surplus solar in EVERY mode (universal
    # self-consumption); with it disabled external solar is exported and the
    # house draws the full base load from grid.  The PV sell/charge slots the
    # optimizer itself plans are the effective switch schedule, so compute
    # them first and feed the per-slot flag into the device simulation (this
    # benchmark = "what the internal plan costs GIVEN our PV handling").
    pv_flags: list[bool] = []
    if enable_pv_strategy:
        pv_flags = _plan_pv_sell_slots(
            cfg, result_slots, solar_15min, buy_prices, sell_prices,
            start_slot=start_slot,
            initial_soc_kwh=current_soc_kwh,
        )
        _correct_soc_for_pv_sells(result_slots, pv_flags, solar_15min, cfg)

    emaldo_cost = 0.0
    e_cycled = 0.0  # battery-internal kWh discharged under the Emaldo plan
    e_import_total_kwh = e_export_total_kwh = 0.0
    e_import_energy = e_import_transfer = e_import_tax = e_import_commission = 0.0
    e_export_energy = e_export_commission = 0.0
    if emaldo_modes is not None:
        e_soc = current_soc_kwh
        rate_ac = cfg.max_charge_per_slot_kwh
        for s in range(n):
            if s < start_slot:
                continue
            if s >= len(emaldo_modes):
                break
            e_bp = buy_prices[s] if s < len(buy_prices) else 0.0
            e_sp = sell_prices[s] if s < len(sell_prices) else 0.0
            mode = emaldo_modes[s]

            net = net_loads[s]  # kW; negative = solar surplus
            surplus_ac = max(0.0, -net) * SLOT_DURATION_HOURS
            deficit_ac = max(0.0, net) * SLOT_DURATION_HOURS
            pv_on = pv_flags[s] if pv_flags else True

            # PV handling: universal surplus charging when third-party PV on
            # (any mode); when off, external solar is sold and the house
            # draws the full base load from the grid.
            if pv_on:
                headroom_ac = max(0.0, (soc_max_kwh - e_soc) / cfg.charge_efficiency)
                solar_charge_ac = min(surplus_ac, rate_ac, headroom_ac)
                e_soc += solar_charge_ac * cfg.charge_efficiency
                e_export_kwh = max(0.0, surplus_ac - solar_charge_ac)
                load_def = deficit_ac
            else:
                solar_charge_ac = 0.0
                e_export_kwh = solar_15min[s] * SLOT_DURATION_HOURS
                load_def = cfg.base_load_kw * SLOT_DURATION_HOURS

            # Mode semantics (grid/discharge policy):
            #   charge  (1) — grid charging allowed: top the battery up to the
            #                max AC rate beyond what solar already put in.
            #   discharge(-1) — battery may discharge to cover the load.
            #   idle    (0) — no grid charge, no discharge; grid covers load.
            e_grid_kwh = 0.0
            if mode == 1:
                grid_charge_ac = max(
                    0.0,
                    min(rate_ac, (soc_max_kwh - e_soc) / cfg.charge_efficiency)
                    - solar_charge_ac,
                )
                e_soc += grid_charge_ac * cfg.charge_efficiency
                e_grid_kwh = grid_charge_ac + load_def
            elif mode == -1:
                draw_ac = min(
                    load_def,
                    max(0.0, (e_soc - soc_min_kwh) / cfg.discharge_efficiency),
                    cfg.max_discharge_kw * SLOT_DURATION_HOURS,
                )
                e_soc -= draw_ac * cfg.discharge_efficiency
                e_cycled += draw_ac
                e_grid_kwh = max(0.0, load_def - draw_ac * cfg.discharge_efficiency)
            else:
                e_grid_kwh = load_def

            e_soc = max(e_soc - cfg.idle_power_kw * SLOT_DURATION_HOURS, 0.0)
            emaldo_cost += e_bp * e_grid_kwh - e_sp * e_export_kwh
            # Decomposed emaldo subcosts.
            e_ie, e_it, e_itx, e_ic = buy_decomp[s]
            e_ee, e_etx, e_ec = sell_decomp[s]
            e_import_total_kwh += e_grid_kwh
            e_export_total_kwh += e_export_kwh
            e_import_energy += e_ie * e_grid_kwh
            e_import_transfer += e_it * e_grid_kwh
            e_import_tax += e_itx * e_grid_kwh
            e_import_commission += e_ic * e_grid_kwh
            e_export_energy += e_ee * e_export_kwh
            e_export_commission += e_ec * e_export_kwh

    # F1: battery wear is a real cost of every discharge kWh.  Report net
    # savings (gross minus wear) so the headline number is honest; the
    # plan-cost sensors follow suit (baseline_cost - net_profit nets wear
    # in).  The cost-breakdown attributes expose the grid and wear
    # components separately for dashboards.
    # Emaldo's plan is netted the same way so the benchmark compares like
    # with like (its schedule also cycles the battery).
    wear_total = cfg.wear_cost_per_kwh * cycled_kwh
    gross_profit = baseline_cost - actual_cost  # positive = savings vs no battery
    emaldo_wear_total = cfg.wear_cost_per_kwh * e_cycled
    emaldo_grid_cost = emaldo_cost  # pre-wear grid cost (net of export revenue)
    emaldo_cost += emaldo_wear_total  # wear is a real cost, netted like the optimizer sensor

    # Baseline gross import cost and net export revenue (F1 breakdown).
    baseline_import_cost = (
        bl_import_energy + bl_import_transfer + bl_import_tax + bl_import_commission
    )
    baseline_export_revenue = bl_export_energy - bl_export_commission

    trace: dict | None = None
    if not _probe:
        trace = {
            "initial_soc_pct": initial_soc_pct,
            "soc_floor_target_pct": cfg.soc_floor_target_pct,
            "case_a_floor": round(case_a_floor, 6),
            "solar_regime_engaged": solar_regime_engaged,
            "future_min_buy": future_min_buy,
            "refill_fraction": round(refill_fraction, 4),
            "solar_full_recharge": solar_full_recharge,
            "first_solar_slot": first_solar_slot,
            "initial_usable_kwh": round(initial_usable_kwh, 3),
            "post_solar_usable_kwh": round(post_solar_usable_kwh, 3),
            "total_discharge_budget": round(total_discharge_budget, 3),
            "night_reserve_kwh": round(night_reserve_kwh, 3),
            "n_profitable_discharge": len(profitable_discharge),
            "n_profitable_charge": len(profitable_charge),
            "mode": "split" if solar_full_recharge else "combined",
            "night_drain_applied": night_drain_plan is not None,
            "grid_charge_needed": round(grid_charge_needed, 3),
        }
        if night_drain_plan is not None and initial_soc_pct is not None:
            trace["edge_pct"] = edge_pct
            trace["excess_kwh"] = round(excess_kwh, 3)

    result = OptimizationResult(
        slots=result_slots,
        total_profit=gross_profit,
        baseline_cost=baseline_cost,
        emaldo_cost=emaldo_cost,
        emaldo_modes=emaldo_modes or [],
        emaldo_grid_cost=emaldo_grid_cost,
        emaldo_wear_total=emaldo_wear_total,
        emaldo_cycled_kwh=e_cycled,
        charge_slots=n_charge,
        discharge_slots=n_discharge,
        idle_slots=n_idle,
        safeguard_slots=sorted(safeguard_slots),
        cycled_kwh=cycled_kwh,
        wear_cost_total=wear_total,
        net_profit=gross_profit - wear_total,
        remaining_slots=n - start_slot,
        baseline_import_kwh=baseline_import_kwh,
        baseline_export_kwh=baseline_export_kwh,
        baseline_import_cost=baseline_import_cost,
        baseline_export_revenue=baseline_export_revenue,
        baseline_import_energy=bl_import_energy,
        baseline_import_transfer=bl_import_transfer,
        baseline_import_tax=bl_import_tax,
        baseline_import_commission=bl_import_commission,
        baseline_export_energy=bl_export_energy,
        baseline_export_commission=bl_export_commission,
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        grid_energy=g_import_energy,
        grid_transfer=g_import_transfer,
        grid_tax=g_import_tax,
        grid_commission=g_import_commission,
        grid_export_energy=g_export_energy,
        grid_export_commission=g_export_commission,
        emaldo_import_kwh=e_import_total_kwh,
        emaldo_export_kwh=e_export_total_kwh,
        emaldo_import_energy=e_import_energy,
        emaldo_import_transfer=e_import_transfer,
        emaldo_import_tax=e_import_tax,
        emaldo_import_commission=e_import_commission,
        emaldo_export_energy=e_export_energy,
        emaldo_export_commission=e_export_commission,
        trace=trace,
    )

    if enable_pv_strategy:
        result.thirdparty_pv_slots = pv_flags

    _LOGGER.info(
        "Optimization complete: savings=%.4f€ (baseline=%.4f, actual=%.4f, emaldo=%.4f), "
        "charge=%d (%d safeguard), discharge=%d, idle=%d slots",
        result.total_profit, baseline_cost, actual_cost, emaldo_cost,
        n_charge, len(safeguard_slots), n_discharge, n_idle,
    )

    return result
