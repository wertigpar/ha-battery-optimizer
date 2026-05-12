# Battery Optimizer — Design & Parameters

## Optimization Targets

The optimizer produces a 96-slot (15-minute) schedule per day. When tomorrow's prices are available, it also produces a separate 96-slot plan for tomorrow. Both are pushed to the battery via a single rolling 96-slot E2E override window. Its main goals, in priority order:

- **Maximize savings from self-consumption** — the Emaldo battery load-matches during discharge (covers household load only, no grid export). Discharge value equals the grid buy price avoided, not the sell/export price. A discharge slot is scheduled when `buy_price > wear_cost`.
- **Maximize free solar self-consumption** — when solar production exceeds household load, capture surplus energy in the battery at zero cost instead of exporting it at a low feed-in price.
- **Round-trip arbitrage** — buy cheap grid energy, store it, discharge later to avoid expensive grid purchases. Only worthwhile when `buy_saved > buy_charged / (η_c × η_d) + wear_cost`.
- **Avoid unnecessary grid charging** — grid charge is limited to the deficit that solar and existing SoC cannot cover.
- **Respect SoC constraints** — never charge above `soc_max` or discharge below `soc_min`.
- **Account for round-trip efficiency losses** — both charge and discharge efficiency are factored into round-trip profitability checks.
- **Account for battery wear cost** — configured directly as `wear_cost_per_kwh` (€/kWh, default **0.03**). Typical LFP range: 1–5 snt/kWh cycled. A flat cost per kWh — no efficiency division needed.
- **Account for battery idle power** — the battery unit draws constant power (default 0.1 kW / 100 W) which drains SoC in every slot. This is subtracted from all SoC projections, solar budget estimates, and discharge energy budgets. At 0.1 kW this equals 2.4 kWh/day (~16 % of 15 kWh).
- **Two-cycle discharge budget** — when solar will fully recharge the battery, overnight discharge is treated as a separate pre-solar cycle. The battery can drain pre-solar AND refill from solar AND drain again post-solar. The discharge budget accounts for both cycles (`initial_usable + post_solar_usable`), preventing overnight high-price slots from being skipped when daytime slots alone would exhaust a single-cycle budget.
- **Prefer discharge at highest buy-price slots first** — greedy assignment from most expensive grid buy price downward (biggest savings first). Discharge candidates are non-solar slots only.
- **Prefer charge at cheapest buy-price slots first** — greedy assignment from cheapest buy price upward.
- **Plan today + tomorrow** — when next-day prices are available (typically after 14:00), the optimizer also plans all 96 slots for tomorrow. Both plans are pushed via a rolling 96-slot E2E window: positions `[now_slot..95]` carry today's plan, positions `[0..now_slot-1]` carry tomorrow's plan.
- **Re-optimize on schedule and events** — fixed midnight checkpoint at 00:01 plus configurable periodic re-runs (15/30/60/120 min, default 120) and immediate re-run when Nordpool publishes new prices. Immediate re-run also triggered when grid balancing ends (FCR-N/mFRR → idle transition). Conditional runs skip if SoC deviation < 10%.
- **Auto-adjust base load** — `base_load_kw` can be auto-tuned from a 7-day rolling average of `load_energy_today` statistics. Exposed as `sensor.battery_optimizer_auto_base_load_kw`. Falls back to configured value when fewer than 3 days of data are available.
- **PV sell strategy** — after the main greedy pass, `_plan_pv_sell_slots()` selects a single **cutover slot T** (≤ noon) and sells all solar before T to the grid (PV switch OFF), then charges the battery uninterrupted from T onward. T is the latest slot where remaining solar after T can still fill the battery to `soc_max`. Two guards protect against activating the strategy on unsuitable days:
  1. **Solar surplus margin** (`pv_sell_solar_margin`, default 1.5): total remaining solar must be ≥ `needed_kwh × margin`. Raising this above 1.0 ensures a genuine solar surplus is forecast before the strategy engages — preventing undercharge on cloudy days.
  2. **Price spread guard** (`pv_sell_min_price_spread`, default 0.03 €/kWh): the average sell price in the morning window must exceed the average cheapest-25% night buy price by at least this margin. Flat-price days with no financial incentive are skipped automatically. Grid-charge slots are never overridden. Enabled via `switch.battery_optimizer_pv_strategy`. After sell slots are planned, `_correct_soc_for_pv_sells()` recomputes the SoC trajectory so the dashboard forecast is accurate.

## Algorithm Overview (Greedy)

1. **Classify slots** — solar surplus (net_load < 0) vs. grid slots (net_load ≥ 0)
2. **Estimate solar budget** — run a forward idle-only SoC simulation to find the peak SoC reachable from solar alone. A parallel simulation starting from `soc_min` computes the two-cycle discharge budget.
3. **Find Case A discharge candidates** — existing stored energy can be discharged for self-consumption when `buy_price > wear_cost` AND `total_discharge_budget > 0`. Discharge candidates are grid slots only (sorted by buy price descending).

   **Two-cycle budget**: `total_discharge_budget = initial_usable_kwh + post_solar_usable_kwh` where `initial_usable = current_soc − soc_min` and `post_solar_usable` is computed from a second forward simulation starting at `soc_min` (maximum solar headroom scenario). This allows overnight discharge slots to be planned even when peak daytime slots are more expensive: overnight discharge frees headroom so solar recharges extra energy. Without solar (winter nights), `post_solar_usable = 0` and the formula reduces to the original single-cycle budget.

   **SPLIT MODE** (solar-full-recharge days): when `post_solar_usable_kwh ≥ 95 %` of the full usable range, the pre-solar and post-solar discharge candidates are allocated from independent pools (`pre_budget = initial_usable_kwh`, `post_budget = post_solar_usable_kwh`). This prevents high-priced daytime slots from consuming the pre-solar budget needed for overnight discharge.

4. **Find Case B round-trip pairs** — grid charge is added when `buy_saved > buy_charged / round_trip + wear_cost` (buy cheap now, discharge later to avoid expensive grid purchases)
5. **Assign discharge** (highest buy price first), then **solar idle** (free energy), then **grid charge** (deficit only). Discharge energy per slot = `min(net_load, max_discharge) × slot_duration` (load-matched, not full-rate)
6. **Simulate SoC** through all 96 slots and build the Emaldo byte schedule
7. **Plan PV sell slots** (when `enable_pv_strategy=True`) — `_plan_pv_sell_slots()` finds a single cutover slot T ≤ noon and marks all solar slots before T as sell-to-grid (if `sell_price > wear_cost`). Solar from T onward is kept for uninterrupted battery charging. Then `_correct_soc_for_pv_sells()` does a forward SoC correction pass. Both today and tomorrow plans go through this step.

## Emaldo Slot Encoding & Battery Behaviour

| Byte Value | Meaning | Grid Draw | Solar Charge |
|---|---|---|---|
| **0** (IDLE) | Force idle — no grid interaction | **No** | **Yes** — absorbs excess solar, exports only when full |
| **1–100** | Charge to N% SoC from any source | **Yes** | Yes |
| **128** | No override — follow built-in AI schedule | AI decides | AI decides |
| **129–255** | Discharge to (256 − N)% SoC | **No** — load-matched, covers household load only | N/A |

**Key insights**:
- IDLE (0x00) is effectively "solar-only charge" — the battery absorbs free solar surplus without drawing from the grid. This makes IDLE the correct command for solar surplus slots.
- Discharge (129–255) is **load-matched** — the battery automatically adjusts its discharge rate to match household load. It does not export to grid during discharge.

---

## Available Parameters & Data Sources

### A. Electricity Price Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 1 | **Spot price (15-min data attribute)** | **YES** — primary input. `data` attribute parsed into 96 buy/sell prices per day. | Configured via `spot_price_sensor`. Template sensor with 15-min resolution `data` list |
| 2 | Nord Pool current price | No | Could be useful for pre-filtering — if highest price < min profitable sell, skip optimization entirely |
| 3 | Average price today | No | Useful as a benchmark — if today's average is very low, profit potential is minimal |

### B. Solar / PV Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 4 | **Solcast today forecast** | **YES** — `detailedForecast` attribute (48 × 30-min slots) interpolated to 96 × 15-min | Configured via `solcast_today_sensor`. Primary solar input |
| 5 | **Solcast tomorrow forecast** | **YES** — same format, used for tomorrow optimization | Configured via `solcast_tomorrow_sensor` |
| 6 | Solcast remaining today | No | Quick check: if remaining solar > battery headroom, discharge before solar arrives |
| 7 | Solar generation today | No | Could compare actual generation vs Solcast forecast to calculate daily forecast accuracy |

### C. Battery Data (Emaldo / Power Store)

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 8 | **Battery SoC** | **YES** — `initial_soc_pct` for optimization, used to calculate starting energy | Configured via `battery_soc_sensor`. Primary battery input |
| 9 | Battery capacity (live) | No — capacity is a **config parameter** | Could auto-calibrate from live sensor |
| 10 | Battery charged / discharged today | **YES** — used for plan-vs-actual accuracy. `_read_emaldo_sensor_float()` reads cumulative totals; `_compute_plan_accuracy()` diffs against SoC-projected kWh for elapsed slots. Exposed as `sensor.battery_optimizer_plan_accuracy` | Useful for deriving round-trip efficiency estimate |
| 11 | Active mode | No | Could verify the battery is actually doing what the optimizer told it to do |

### D. Grid Meter Data

| # | Parameter | Used in Optimizer | Notes |
|---|---|---|---|
| 12 | Grid power total | No | Could validate that solar surplus slots are correctly identified |
| 13 | 15-min grid energy usage | No | Matches optimizer slot resolution — could be used for real-time plan-vs-actual comparison |

### E. Battery Telemetry (E2E Protocol — available but not exposed as HA sensors)

| # | Parameter | Potential Value |
|---|---|---|
| 14 | Cycle count | Could auto-update wear cost based on actual cycles |
| 15 | State of Health (SoH %) | Adjusts effective capacity — a battery at 80% SoH has less usable capacity |
| 16 | BMS temperature (°C) | Cold temperatures reduce capacity and efficiency — relevant for Nordic climate |
| 17 | Fault bits | Should disable optimization if faults detected |

---

## Current Simplifications & Assumptions

| Assumption | Reality | Impact |
|---|---|---|
| Flat household load (`base_load_kw`, configurable) | Load varies by time of day. Auto-tune adjusts to 7-day rolling average; per-slot profile not yet modelled | Daily average now adaptive; per-slot solar surplus still approximated |
| Fixed charge/discharge efficiency | Efficiency depends on power level, SoC, and temperature | Minor — LFP batteries are fairly flat across SoC range |
| Linear battery wear model | Real degradation depends on SoC, temperature, C-rate, cycling depth | Minor for LFP at moderate cycling rates |
| Solcast forecast is accurate | Clouds cause 50-80% forecast errors on individual 30-min slots | Can lead to over-reliance on solar, leaving discharge slots un-covered |
| No grid export limits | Some grid connections have export caps | Could lead to curtailment — planned discharge revenue never materializes |
| Today and tomorrow planned independently | Both days are fully planned with their own prices and solar forecast. However, end-of-day SoC carryover is not jointly optimized — today's discharge is chosen solely based on today's prices regardless of tomorrow's peak. Storing cheap energy tonight for a high-priced tomorrow morning is not modelled. | Lost cross-day arbitrage; carryover SoC may not be optimal |

---

## Plan Accuracy Sensor

`sensor.battery_optimizer_plan_accuracy` provides lightweight plan-vs-actual tracking:

- **State**: signed discharge error in kWh (positive = more discharge than planned, negative = less)
- **Implementation**: `_compute_plan_accuracy()` in coordinator compares planned SoC-delta × capacity to actual `battery_charged/discharged_today` cumulative sensor deltas across elapsed slots since the last optimizer run
- **Attributes**: full planned vs actual breakdown for discharge, charge, and solar
