# Changes

## Unreleased

### Fixed

- **Double idle drain on solar-surplus discharge slots** — a discharge action
  on a solar-surplus slot subtracted idle drain twice in the result pass (once
  in the surplus branch, once on the shared post-slot line), so the forecast
  SoC drifted one idle drain per slot below `_simulate_soc_trajectory`. The
  surplus branch now absorbs the surplus and drains once, matching the
  simulation.
- **Uneconomical keep-alive top-ups above the floor** — the SoC safeguard
  inserted `charge_floor` slots at the cheapest prices even when the battery
  already sat at/above the floor target at that slot (no-op: no energy added,
  grid cost incurred). Keep-alive inserts and rescue flips are now filtered by
  a projected deficit — `_simulate_soc_trajectory` derives the SoC entering
  each candidate slot and only slots below the floor target qualify; a rescue
  never cancels a discharge that starts above the floor.
- **Discharge over-commit from the two-cycle budget** — `initial_usable +
  post_solar_usable` can exceed physical capacity above the floor, so an
  at-budget plan drained below `soc_min + reserve`. After grid charging, the
  optimizer forward-simulates the planned trajectory and drops the cheapest
  committed discharge slot (re-simulating after each drop, bounded at 96
  iterations) until the projected min SoC holds the floor target. Runs before
  the safeguard so no keep-alive top-up is layered on a floor-violating plan.
- **Dead `solar_full_recharge` gate killed the overnight-discharge pool** —
  the split-mode gate compared `post_solar_usable_kwh` (peak from floor minus
  `soc_min` minus the discharge reserve) against 95 % of the full usable band,
  so the threshold sat ~0.1 kWh above physical capacity: it could never fire
  and two-cycle mode was permanently dead — the cheapest overnight slots were
  starved on days when solar fully recharges the battery. The gate now compares
  the simulated peak SoC from the floor against the band top
  (`soc_min + 0.95 * band`), which is reachable when solar truly refills the
  battery. Verified by A/B against live arrays: on a full-recharge day the
  fixed gate fires and unlocks +0.1135 €/day (night pool 11→16 slots, morning
  32→22, evening unchanged); on the p10 forecast day the gate correctly stays
  closed (battery only refills to ~76 % from the floor) and the plan is
  unchanged.
- **Starved night slots on COMBINED days (no/partial solar)** — the single
  discharge pool covers evening → morning → night LAST, so the cheapest night
  grid buys are never displaced. Day profit is flat over a range of starting
  SoCs (the plateau): stored energy above the plateau edge is dead. The
  optimizer now probes the day profit as a function of start SoC (binary
  search, ~6 runs, edge guarded at floor + recovery + margin), and when the
  day starts above the edge it discharges the dead excess overnight (buy >
  wear cost, pre-solar window, slots sorted buy-desc) before solar onset,
  displacing grid buys without touching the day plan. Live p10 forecast day:
  +0.264 €/day (0.4457 → 0.7097); drain-to-floor regression stays negative
  so the edge stop is mandatory. Disabled when the plan starts past solar
  onset or the battery starts below floor + recovery + margin.

## v0.2.3

### Fixed

- **Sustained sub-`soc_min` battery dips from committed plans** — when a
  planned discharge run ended exactly at the floor, overnight idle drain
  pulled the projected SoC 3.3–4.0 % below `soc_min` for the rest of the
  night (live 2026-08-05: sauna drain + no-arbitrage day, 42 warnings in
  2 h). The safeguard could not fix it because every slot up to the
  violation was already committed. Two-part fix in optimizer.py:
  - **Discharge reserve** — the greedy pass's dischargeable bottom edge is
    now `soc_min + soc_recovery_buffer_pct`, not `soc_min` itself (budget
    sites `initial_usable_kwh`, `post_solar_usable_kwh`, and the grid-charge
    balance's `existing_usable`). The last discharge slot stops at the
    reserve line; the buffer absorbs idle drain before the floor is touched.
    Keep-alive charging already targets `soc_min + buffer`, so both sides
    are consistent.
  - **Rescue override** — when the safeguard finds no free slot before a
    violation, it now flips the cheapest committed discharge slot inside the
    violated window into a `charge_floor` slot instead of skipping (new
    `_find_rescue_slot` helper; cheapest by buy price, closest to the
    violation on ties; only the cheapest slot is sacrificed, deeper dips
    flip more over successive iterations). The "no free slot" skip-log path
    remains for windows with no discharge slot to sacrifice.

### Changed

- **Repro harness budget shift** — the reserve holds ~5 % of capacity out of
  the discharge budget, so the documented repro.py profit moved 0.7797 →
  0.7407 on the same captured day (evening-peak discharge cut; morning slots
  unaffected). repro2/repro3 (PV-sell gate) results unchanged: sell slots,
  max SoC 99.8 %, profits 0.0292/0.1151/0.2011/0.6308 identical.
- Bump `manifest.json` → `0.2.3`.

## v0.2.2

### Fixed

- **Plan accuracy across midnight windows** — the midnight optimizer run
  (00:00) snapshots the Emaldo daily-reset counters (`battery_discharged_today`,
  `battery_charged_today`, `solar_energy_today`) before the device-side reset
  lands, so the next accuracy comparison saw a negative delta and the guard in
  `_compute_plan_accuracy` silently dropped the entire window's actuals (live
  Plan Accuracy sensor showed planned values with no `actual_*_kwh`). Such a
  window now records the post-reset accumulation as best-effort actual and
  flags it (`<action>_reset_crossed: True` in the sensor attrs) instead of
  dropping it, so midnight windows keep a measurable — if partial — actual.
- **SoC safeguard UnboundLocalError** — `_soc_floor_warn_depth` is module
  state read and written inside `_apply_soc_safeguard`; without a `global`
  declaration the assignments shadowed it and the first run hitting the
  skip branch raised `UnboundLocalError` (task exception on every optimizer
  run). Fixed with a `global` declaration.

### Changed

- **SoC safeguard log noise** — the "no free slot for a keep-alive
  charge" message was logged at `WARNING` for any dip > 1 % below the
  floor, spamming the log on cloudy/no-arbitrage days (170 occurrences
  in 4 h observed in one session). Dips < 3 % below the floor are now
  logged at `INFO` (routine, self-resolving: weather/price-driven, no
  action needed); deeper dips still at `WARNING`. Per-episode dedup
  (optimizer.py `_soc_floor_warn_depth`) logs the message only when the
  dip depth grows, and resets when a run finds the day clean, so a new
  episode logs fresh.
- Bump `manifest.json` → `0.2.2`.

## v0.2.1

### Added

- **Local brand images** (`brand/` folder): original battery-glyph icon + logo (navy `#112A41` icon, energy-green `#00B25C` logo, white dark-mode variants, all `@2x`). Distinct from Emaldo branding — no Emaldo logo used. Served via HA 2026.3+ local brands proxy (`/api/brands/integration/battery_optimizer/...`).
- **Persistable plan accuracy history** — per-run planned-vs-actual records
  (discharge/charge/solar kWh + signed errors) are now written to
  `battery_optimizer_accuracy.json` in the HA config dir and survive HA
  restarts. The HA recorder strips sensor attributes, so the rolling trend
  was previously uncollectable. A rolling summary (`runs`, `window_days`,
  `mean_solar_error_kwh`, `solar_under/over_forecast_runs`,
  `mean_discharge_error_kwh`) is injected into the **Plan Accuracy** sensor's
  `accuracy_history` attribute. Sign convention: `error = actual − planned`;
  negative solar error = forecast over-optimistic (actual below forecast),
  positive = forecast conservative (actual above forecast, e.g. P10). Cap:
  1000 records / 60 days, oldest dropped first. Purely observational — no
  planning behaviour changes. Enables long-term solar-forecast bias tracking
  (e.g. verifying P10 vs P50 drift) before any forecast-mode change.

## v0.2.0

### Added

- **HACS compatibility** — `hacs.json`, HACS badges + "Open HACS repository"
  button in README, GitHub links (`documentation`, `issue_tracker`,
  `codeowners`) in manifest.json, MIT `LICENSE`, `brand/icon.png`, repo topics,
  and `after_dependencies: recorder` (auto base-load history query). HACS
  action (topics/license/brands) and hassfest validation both green.

- **Tomorrow cost sensors** — New sensors (`Tomorrow Estimated Savings`,
  `Tomorrow Baseline Cost`, `Tomorrow Emaldo Cost`, `Tomorrow Optimizer Cost`)
  showing tomorrow's estimates after Nordpool publishes (~14:00). Read
  `coordinator.last_result_tomorrow`; show `unknown` before data is available.

### Fixed

- **PV sell strategy starvation** — the cutover scan used the plan-start SoC
  (`needed_kwh = soc_max − current_soc`), which understates the battery gap
  accumulated during the sell window (missed surplus absorption + base-load
  coverage). On sunny days the battery peaked ~78 % instead of 100 %, losing
  ~€0.40/day in evening-peak discharge. The cutover T is now derived by
  iteration: `_forward_soc_sim()` simulates the SoC entering the cutover under
  the sell window, the true need is re-derived, and T is re-scanned until
  stable (max 6 passes). A final starvation guard re-validates T against the
  re-simulated need and skips selling for the day if solar after T cannot
  cover it. Floor recovery now precedes selling: below the SoC floor, solar
  recharges to `soc_min + buffer` before any export; if unrecoverable, selling
  is skipped.

- **Cost sensor baseline accumulated past slots** — Baseline cost calculation
  (optimizer.py `for s in range(n)`) counted all 96 slots without guarding by
  `start_slot`. Past slots inflated `baseline_cost` by up to ~44% over the
  remaining-day true baseline. Fixed by wrapping the accumulation in
  `if s >= start_slot:`.

- **Config UI fixes** — Various configuration flow and options flow corrections
  for field validation and defaults.

- **Issue #2 fix** — [details TBD]

- **Issue #1 fix** — Battery discharge safeguard edge case corrections.

- **Negative spot price VAT clamp** — When `spot < 0`, VAT multiplier is clamped
  to 1.0 so the subsidy passes through at face value without amplification.

- **Algorithm stability** — Solcast fallback, idle slot strategy edge cases, and
  SoC guard interval timing corrections.

### Changed

- **PV sell economic gate** — slots are only marked sell when the sell price
  exceeds the slot's buy price (`sell_price > buy_price`), in addition to the
  existing `pv_sell_min_price_spread` floor. Selling below the buy price loses
  money because a stored kWh displaces a future grid buy through the
  round-trip. Direct PV export incurs zero battery wear, so no wear-cost term
  is applied.

- **Cost sensor naming** — 4 cost/savings sensors prefixed with `Rest of Day`
  to clarify estimate scope (rest of today, not full day).

- **Emaldo schedule chart time scale** — Now includes tomorrow (day=1) slots
  from `coordinator.last_result_tomorrow` when available, matching
  `ScheduleChartSensor` time axis for aligned chart overlays.

- **Idle slot strategy** — Added `full_control`, `solar_guard`, `smart_override`
  strategies for controlling battery idle behaviour (initial commit).

- **Internal Nordpool sensor support** — Can read spot prices directly from
  Emaldo integration instead of requiring a separate price sensor.

- **Auto base load** — Recorder-based rolling 7-day average from household load
  sensor with ±50% clamp.

- **PV sell strategy** — Morning solar-to-grid arbitrage with configurable
  cutover time, solar margin guard, and min sell price threshold.

- **Control disable switch** — `switch.battery_optimizer_emaldo_control_enable`
  to test/dry-run without sending commands to the battery.

- **Third-party PV reconciliation** — After each optimizer recalculation, the
  Emaldo third-party PV switch is reconciled to the effective current plan.

- **Many refinements** — Round-trip trade pricing (efficiency + wear cost),
  supplier emission attribution, error suppression during HA startup phase,
  battery discharge safeguards, cloudy-day charging plan, Solcast p10/p50 mode.

