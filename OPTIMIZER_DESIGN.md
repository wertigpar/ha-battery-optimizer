# Battery Optimizer — Design & Parameters

## Optimization Targets

The optimizer produces a 96-slot (15-minute) schedule per day. When tomorrow's prices are available, it also produces a separate 96-slot plan for tomorrow. Both are pushed to the battery via a single rolling 96-slot E2E override window. Its main goals, in priority order:

- **Maximize savings from self-consumption** — the Emaldo battery load-matches during discharge (covers household load only, no grid export). Discharge value equals the grid buy price avoided, not the sell/export price. A discharge slot is scheduled when `buy_price > wear_cost`.
- **Maximize free solar self-consumption** — when solar production exceeds household load, capture surplus energy in the battery at zero cost instead of exporting it at a low feed-in price.
- **Round-trip arbitrage** — buy cheap grid energy, store it, discharge later to avoid expensive grid purchases. Only worthwhile when `buy_saved > buy_charged / (η_c × η_d) + wear_cost`.
- **Avoid unnecessary grid charging** — grid charge is limited to the deficit that solar and existing SoC cannot cover.
- **Respect SoC constraints** — never charge above `soc_max` or discharge below `soc_min + soc_recovery_buffer_pct` (the **discharge reserve**: the greedy pass stops discharge at `soc_min + buffer`, not at `soc_min`, so a planned run never ends exactly at the floor with no headroom for idle drain).
- **SoC floor safeguard (keep-alive charging)** — the battery unit's idle consumption (~0.1 kW) drains SoC continuously, even below `soc_min` (IDLE mode forbids grid draw). After the greedy pass, an **unclamped** forward SoC simulation detects projected floor violations. For each violation, a minimal `charge to (soc_min + buffer)%` slot is inserted at the cheapest buy-price slot at or before the violation (ties broken toward the violation for maximum headroom per top-up) — **but only when that slot projects a real deficit**: the simulated SoC entering the slot must sit below the floor target, so no no-op top-up is bought when the battery already holds the floor (flat days stay charge-free). This is a constraint, not a trade — no profitability test — but cost is minimised: only the deficit energy (a few hundred Wh) is bought. **Rescue override**: when no deficit free slot exists before the violation, the cheapest committed discharge slot that *starts below* the floor target is flipped into a `charge_floor` slot (cheapest by buy price, closest to the violation on ties) — a discharge that begins above the floor is never cancelled to top up a floor that is not violated; deeper dips flip more slots over successive iterations. A **discharge over-commit correction** runs before this pass: the two-cycle budget can double-count the battery, so the cheapest committed discharge slot is dropped (and the trajectory re-simulated) until the projected min SoC holds the floor target — the safeguard then never layers a keep-alive charge on top of a plan that already violates the floor. Configurable via `enable_soc_safeguard` (default on) and `soc_recovery_buffer_pct` (default 5). The SoC trajectory shown on the dashboard is no longer clamped at `soc_min`, so real sub-floor drift is visible when the safeguard is off.
- **Low-SoC replan trigger** — a state watcher on the battery SoC sensor forces an immediate re-run (throttled to once per 30 min) when actual SoC drops below `soc_min + 2 %` and the current plan has no charge slot within the next 2 hours, letting the safeguard insert a keep-alive charge based on real conditions.
- **Account for round-trip efficiency losses** — both charge and discharge efficiency are factored into round-trip profitability checks.
- **Account for battery wear cost** — configured directly as `wear_cost_per_kwh` (€/kWh, default **0.03**). Typical LFP range: 1–5 snt/kWh cycled. A flat cost per kWh — no efficiency division needed. The result reports `cycled_kwh`, `wear_cost_total` and `net_profit` (`total_profit − wear_cost_total`); savings sensors surface the net figure so the headline number is honest, while plan-cost sensors use `baseline_cost − net_profit`. The Emaldo benchmark cost is netted for its own cycles so the comparison is like-for-like.
- **Account for battery idle power** — the battery unit draws constant power (default 0.1 kW / 100 W) which drains SoC in every slot. This is subtracted from all SoC projections, solar budget estimates, and discharge energy budgets. At 0.1 kW this equals 2.4 kWh/day (~16 % of 15 kWh).
- **Two-cycle discharge budget** — when solar will fully recharge the battery, overnight discharge is treated as a separate pre-solar cycle. The battery can drain pre-solar AND refill from solar AND drain again post-solar. The discharge budget accounts for both cycles (`initial_usable + post_solar_usable`), preventing overnight high-price slots from being skipped when daytime slots alone would exhaust a single-cycle budget.
- **Prefer discharge at highest buy-price slots first** — greedy assignment from most expensive grid buy price downward (biggest savings first). Discharge candidates are non-solar slots only.
- **Prefer charge at cheapest buy-price slots first** — greedy assignment from cheapest buy price upward.
- **Plan today + tomorrow** — when next-day prices are available (typically after 14:00), the optimizer also plans all 96 slots for tomorrow. Both plans are pushed via a rolling 96-slot E2E window: positions `[now_slot..95]` carry today's plan, positions `[0..now_slot-1]` carry tomorrow's plan.
- **Re-optimize on schedule and events** — fixed midnight checkpoint at 00:01 plus configurable periodic re-runs (15/30/60/120 min, default 120) and immediate re-run when Nordpool publishes new prices. Immediate re-run also triggered when grid balancing ends (FCR-N/mFRR → idle transition). Conditional runs skip if SoC deviation < 10%. Two further replan triggers sit on top of the polled checkpoints. **Idle-gap gate** (`_should_reoptimize`, throttled 30 min): when the plan leaves the current slot idle while the grid actually buys at a price above wear cost and the battery has headroom, a re-run with the real (often higher) SoC lets the discharge allocation open slots the plan-time projection kept closed — the fix for low-load days where the battery fills early and earlier discharge slots become worthwhile. **SoC-divergence watcher** (`_on_soc_state_change`, throttled 15 min, independent of `enable_soc_safeguard`): when actual SoC deviates > 5 % from the plan's projected slot SoC, force a replan — catches forecast error, unexpected loads, and cheap-day under-discharge between checkpoints.
- **Auto-adjust base load** — `base_load_kw` can be auto-tuned from a 7-day rolling average of `load_energy_today` statistics. Exposed as `sensor.battery_optimizer_auto_base_load_kw`. Falls back to configured value when fewer than 3 days of data are available.
- **PV sell strategy** — after the main greedy pass, `_plan_pv_sell_slots()` selects a single **cutover slot T** (≤ noon) and sells all solar before T to the grid (PV switch OFF), then charges the battery uninterrupted from T onward. The cutover is found by **iteration**: `_forward_soc_sim()` simulates the SoC at the candidate cutover under the sell window (the plan-start SoC understates the gap — the battery misses surplus absorption yet still covers base load), re-derives the true need (`soc_max − simulated SoC`), and re-scans T until stable (max 6 iterations). A **final starvation guard** re-validates T against the re-simulated need — if solar after T cannot cover it, selling is skipped for the day. **Charge-to-floor-first**: when the battery enters the window below `soc_min + buffer` (e.g. after a cloudy night), the first solar charges the battery back to the floor before any selling starts; if the floor cannot be reached before the cutover, selling is skipped for the day. **Economic gate**: a slot is only marked sell when `sell_price > sell_threshold` (and above the `pv_sell_min_price_spread` config floor), where `sell_threshold = min(cheapest future discharge buy) × η_c × η_d × pv_sell_margin` (fallback to the slot's own buy price when the plan has no discharge slots) — exporting only beats storing because a stored kWh displaces a future grid buy, so the threshold is the cheapest avoided buy discounted by round-trip efficiency plus a configurable margin (`pv_sell_margin`, default 1.0); direct PV export incurs zero battery wear, so no wear-cost term applies. Grid-charge slots are never overridden. Enabled via `switch.battery_optimizer_pv_strategy`. After sell slots are planned, `_correct_soc_for_pv_sells()` recomputes the SoC trajectory so the dashboard forecast is accurate.
- **Battery-to-grid arbitrage** _(future enhancement)_ — the Emaldo HA component supports exporting stored battery energy to the grid (user sets kWh amount + enables selling). This is not yet planned by the optimizer. When implemented, a grid-sell slot would be profitable when `(spot − commission) > (stored_energy_cost / η_d) + wear_cost`. This threshold is higher than battery-to-house discharge (which avoids a grid buy) because it requires clearing commission, round-trip losses, and wear on top of the original storage cost.

## Algorithm Overview (Greedy)

1. **Classify slots** — solar surplus (net_load < 0) vs. grid slots (net_load ≥ 0)
2. **Estimate solar budget** — run a forward idle-only SoC simulation to find the peak SoC reachable from solar alone. A parallel simulation starting from `soc_min` computes the two-cycle discharge budget.
3. **Find Case A discharge candidates** — existing stored energy can be discharged for self-consumption when `buy_price > wear_cost` AND `total_discharge_budget > 0`. Discharge candidates are grid slots only (sorted by buy price descending).

   **Two-cycle budget**: `total_discharge_budget = initial_usable_kwh + post_solar_usable_kwh` where `initial_usable = current_soc − soc_min − reserve` and `post_solar_usable` is computed from a second forward simulation starting at `soc_min` (maximum solar headroom scenario), each minus `reserve = capacity × soc_recovery_buffer_pct / 100` — the **discharge reserve** (A). The dischargeable bottom edge is `soc_min + buffer`, not `soc_min`: a planned run ending exactly at the floor leaves no idle-drain headroom (sauna-night episode: 3.3–4.0 % sub-floor overnight). The reserve makes the last discharge slot stop at the reserve line, and the buffer absorbs overnight idle drain before the floor is touched. This allows overnight discharge slots to be planned even when peak daytime slots are more expensive: overnight discharge frees headroom so solar recharges extra energy. Without solar (winter nights), `post_solar_usable = 0` and the formula reduces to the original single-cycle budget. **Caveat**: the two cycles share the same physical capacity above the floor, so the budget can over-commit the battery — step 5a drops the cheapest discharge slot until the projected min SoC holds the floor.

   **SPLIT MODE** (solar-full-recharge days): when `post_solar_usable_kwh ≥ 95 %` of the full usable range, the pre-solar and post-solar discharge candidates are allocated from independent pools (`pre_budget = initial_usable_kwh`, `post_budget = post_solar_usable_kwh`). This prevents high-priced daytime slots from consuming the pre-solar budget needed for overnight discharge.

4. **Find Case B round-trip pairs** — grid charge is added when `buy_saved > buy_charged / round_trip + wear_cost` (buy cheap now, discharge later to avoid expensive grid purchases)
5. **Assign discharge** (highest buy price first), then **solar idle** (free energy), then **grid charge** (deficit only). Discharge energy per slot = `min(net_load, max_discharge) × slot_duration` (load-matched, not full-rate). Battery SoC decreases by `discharge_kwh / η_d` per slot — the inverter draws more energy from the cells than it delivers to the house. Both the discharge budget and the grid-charge-needed calculation use battery-internal kWh (`delivered_kwh / η_d`) throughout, so the energy balance is consistent.
5a. **Discharge over-commit correction** — `_simulate_soc_trajectory()` forward-simulates the full planned trajectory; while its min SoC sits below the floor target, the cheapest committed `discharge` slot (lowest buy price, latest slot on ties) is dropped and the trajectory re-simulated (bounded at 96 iterations). The two-cycle budget double-counts the battery, so an at-budget plan would otherwise drain below `soc_min + reserve`.
5b. **SoC floor safeguard** — `_apply_soc_safeguard()` runs an unclamped forward simulation of the planned actions and inserts `charge_floor` slots (byte = `soc_min + buffer`) at the cheapest prices until the projected   trajectory never violates `soc_min` — **only at candidate slots whose projected SoC before the slot is below the floor target** (no no-op top-ups above the floor). **Rescue override**: when no deficit free slot exists before a violation, the cheapest committed discharge slot that starts below the floor target is flipped to `charge_floor` (cheapest by buy price, closest to the violation on ties). Inserted/flipped slots are reported in `OptimizationResult.safeguard_slots` and the status sensor's `safeguard_slots` attribute.
5c. **Plateau-aware overnight drain** (COMBINED days, `enable_night_drain=True` default) — when solar cannot fully refill the battery, the single discharge pool covers evening → morning → night LAST and the cheapest night grid buys are starved. Day profit is flat over a range of starting SoCs (the plateau); stored energy above the plateau edge is dead. Before the discharge pass, the optimizer probes the day profit as a function of start SoC (`_find_plateau_edge`, binary search, ~6 runs, tolerance 0.005 €, edge never below `floor target + recovery + margin`). When the day starts above the edge, the dead excess (`ΔSoC × capacity`) is discharged overnight (`_build_night_drain_plan`): pre-solar window `[start_slot, first_solar_slot)`, only slots the edge plan leaves idle/none with `net_load > 0` and `buy > wear_cost`, sorted buy-desc, per-slot draw `load/η_d` (battery-internal, so the trajectory mirrors the edge plan's). The merged plan replaces the plain one; the night slots then sit above the edge plan's own floor. Disabled inside probes (`_probe=True`), when the plan starts past solar onset, when the battery starts below `floor + recovery + 1 pp`, or when no night slot can absorb any excess.
6. **Simulate SoC** through all 96 slots and build the Emaldo byte schedule. Per slot: charge adds `charge_kw × slot_duration × η_c`; keep-alive charge adds only the deficit up to the floor target; discharge subtracts `delivered_kwh / η_d` **capped at the floor marker** (the discharge byte stops at `soc_min`) plus `idle_drain` (a discharge action on a solar-surplus slot instead absorbs the surplus — adds `solar_kwh`, subtracts idle drain once, never twice); idle subtracts `idle_drain`. The trajectory is clamped at the physical 0 %, not at `soc_min`. After the idle loop, the grid-charge balance also credits surplus absorption on **discharge-assigned** surplus slots (the Emaldo firmware charges from excess solar even in discharge mode), scaled by `solar_forecast_margin` (default 0.85) and capped by the remaining headroom — conservative, so an over-forecast never leaves the plan grid-charge-starved.
7. **Plan PV sell slots** (when `enable_pv_strategy=True`) — `_plan_pv_sell_slots()` finds a single cutover slot T ≤ noon via iterated true-need simulation (floor recovery first, then up to 6 sim-and-rescan passes), validates it with a final starvation guard, and marks solar slots before T as sell-to-grid only where `sell_price` beats the stored value of that kWh — the cheapest future discharge buy discounted by round-trip efficiency and `pv_sell_margin` (economic gate; direct PV export incurs no wear cost). Solar from T onward is kept for uninterrupted battery charging. Then `_correct_soc_for_pv_sells()` does a forward SoC correction pass. Both today and tomorrow plans go through this step.

## User Schedule Layer (rules-as-mask)

User-configured rules (config subentries) select, per 15-min slot, which
of three sources governs the battery: optimizer plan (default), the
battery's internal AI (byte 128), or a manual action (charge@N / idle /
discharge@N). Precedence: date > weekday > default, structural. Same-level
overlap is blocked at creation. After `optimize()` produces the plan, the
coordinator expands the rules per day (`expand_day`), masks the 96-byte
array and the PV plan (`mask_plan`), and re-simulates the SoC trajectory
so tomorrow's start SoC, the SoC guard floors, plan accuracy and the
dashboard forecast all reflect the masked plan. The SoC guard marker
additionally honours user `discharge@N` floors in its look-ahead window.
The optimizer itself is untouched.

## Price Calculation

Effective buy and sell prices (€/kWh) are derived from raw Nordpool spot prices by `compute_prices()`:

| Price type | Formula | Notes |
|---|---|---|
| **Buy price** | `spot × VAT_multiplier + transfer_fee_buy` | When `spot < 0`, `VAT_multiplier` is clamped to `1.0` — negative spot subsidies pass through at face value, not amplified |
| **Sell price** | `spot − sales_commission` | Commission always subtracted; sell price can go negative in extreme markets |

**Default values**: `VAT_multiplier = 1.255` (Finnish 25.5 % electricity VAT), `transfer_fee_buy = 0.0572 €/kWh`, `sales_commission = 0.002 €/kWh`. All configurable.

**Negative spot prices**: Nordpool spot can go negative (typically −0.01 to −0.10 €/kWh during excess wind/nuclear periods). The optimizer treats a negative effective buy price as a strong incentive to charge from the grid. Without the VAT clamp, the multiplier (1.255) would amplify the negative subsidy beyond what consumers actually receive on their electricity bill.

**Per-slot profit estimate** (discharge): `profit = (buy_price − wear_cost_per_kwh) × discharge_kwh`, where `discharge_kwh` is energy delivered to the house. `wear_cost_per_kwh` is configured in € per delivered kWh, so no efficiency division is applied to the profit formula itself — only to the SoC/budget energy balance.

**Cost decomposition** — every plan-cost sensor exposes the components of its state value as attributes (`optimizer_plan_cost_breakdown()`, `emaldo_plan_cost_breakdown()`, `baseline_cost_breakdown()` in optimizer.py). The effective prices are decomposed per kWh back into their economic parts, matching the Finnish household model:

- **Import**: `energy` (spot × VAT), `transfer` (flat grid fee), `tax` (VAT on energy only), `commission` (sales margin). Sum = buy price.
- **Export**: `energy` (spot) and `commission` (sales margin). No export tax, no export transfer. Net revenue = energy − commission.
- **Import tax is zero when the spot price is negative** (same clamp as `compute_prices()`) — the subsidy passes through without amplification, and no VAT is charged on a negative energy price.
- Decomposition is derived from the already-computed buy/sell prices (`_decompose_buy`/`_decompose_sell`), so it needs no new config and is exact by construction: component sums equal the sensor state value (rounding 4 dp money / 3 dp kWh).

**Baseline definition** (no-battery comparison): the baseline is the cost of buying the **full base load** from the grid in every slot (`base_load_kw × slot_duration` at the buy price) minus the revenue from exporting **solar surplus above base load** at the sell price. It deliberately does not net solar against load — without a battery, the household consumes the full base load from the grid regardless of solar, and only surplus generation is exported. This keeps the baseline positive on sunny days (a stored kWh displaces a future grid buy, so the baseline must reflect what the grid would have sold) and makes `savings = baseline − actual` a genuine measure of avoided purchases. The Emaldo benchmark applies the same definition to its idle slots.

## Emaldo Slot Encoding & Battery Behaviour

| Byte Value | Meaning | Grid Draw | Solar Charge |
|---|---|---|---|
| **0** (IDLE) | Force idle — no grid interaction | **No** | **Yes** — absorbs excess solar, exports only when full |
| **1–100** | Charge to N% SoC from any source | **Yes** | Yes |
| **128** | No override — follow built-in AI schedule | AI decides | AI decides |
| **129–255** | Discharge to (256 − N)% SoC | **No** — load-matched, covers household load only | N/A |
| **Battery sell** _(future)_ | Set kWh amount + enable sell mode via Emaldo service | **Yes** — exports stored energy to grid at spot price | N/A |

**Key insights**:
- IDLE (0x00) is effectively "solar-only charge" — the battery absorbs free solar surplus without drawing from the grid. This makes IDLE the correct command for solar surplus slots.
- Discharge (129–255) is **load-matched** — the battery automatically adjusts its discharge rate to match household load. It does not export to grid during discharge.
- **Battery-to-grid sell** is a separate Emaldo API mechanism (not a slot byte). The HA component exposes this as a service: set kWh to sell + enable selling. The optimizer does not yet plan this; see Feature 5 in `FEATURES_PLAN.md`.

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
| 5b | **Forecast accuracy history** | **YES** — `solar_forecast_scale` (config, default `0` = auto). Auto: EWMA over raw-basis ratios `(actual/planned) × scale_used` from `battery_optimizer_accuracy.json`, clipped to 0.3–1.2, applied as a whole-day multiplier to both today and tomorrow forecasts at the `_get_solcast_forecast()` choke point. Manual value disables auto-tune. | Compensates systematic over-forecast bias (e.g. Finland summer cloud patterns) |
| 5c | **Actual solar counter (external)** | **YES** — optional `solar_actual_sensor` (default empty). When set, plan-accuracy `actual_solar_kwh` is diffed from this cumulative counter (Wh→kWh normalized) instead of the Emaldo-internal estimate, which is balance-derived and load-sensitive. Empty = Emaldo estimate. | Configure via options. Counter must be a cumulative daily yield (midnight reset windows are flagged `solar_reset_crossed` and excluded from the auto-tune). Unavailable counter at accuracy time skips the record (no source mixing) |
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
| Solcast forecast is accurate | Clouds cause 50-80% forecast errors on individual 30-min slots. Since v0.2.4 the whole-day forecast is auto-scaled by an EWMA over measured raw-basis accuracy ratios (config `solar_forecast_scale`, default auto) to compensate systematic over-forecast bias. | Mitigated for systematic bias; day-level cloud errors still require `p10` mode + the PV-sell cloudy-day guard |
| No grid export limits | Some grid connections have export caps | Could lead to curtailment — planned discharge revenue never materializes |
| Battery-to-grid export not planned | The Emaldo component supports selling stored battery energy to the grid (kWh amount + enable). The optimizer never schedules this. | Arbitrage opportunities that clear round-trip losses and wear cost are missed; see Feature 5 in `FEATURES_PLAN.md` for design |
| Today and tomorrow planned independently | Both days are fully planned with their own prices and solar forecast. However, end-of-day SoC carryover is not jointly optimized — today's discharge is chosen solely based on today's prices regardless of tomorrow's peak. Storing cheap energy tonight for a high-priced tomorrow morning is not modelled. | Lost cross-day arbitrage; carryover SoC may not be optimal |

---

## Plan Accuracy Sensor

`sensor.battery_optimizer_plan_accuracy` provides lightweight plan-vs-actual tracking:

- **State**: signed discharge error in kWh (positive = more discharge than planned, negative = less)
- **Implementation**: `_compute_plan_accuracy()` in coordinator compares planned SoC-delta × capacity to actual `battery_charged/discharged_today` cumulative sensor deltas across elapsed slots since the last optimizer run
- **Attributes**: full planned vs actual breakdown for discharge, charge, and solar
- **Persisted history**: each run's planned-vs-actual record is appended to `battery_optimizer_accuracy.json` (HA config dir, capped at 1000 records / 60 days) because the HA recorder strips sensor attributes. A rolling `accuracy_history` summary (`runs`, `window_days`, `mean_solar_error_kwh`, `solar_under/over_forecast_runs`, `mean_discharge_error_kwh`) is injected into the sensor's attributes each run. Purpose: long-term solar-forecast bias tracking (P10 vs P50 drift) before any forecast-mode change. Purely observational.
