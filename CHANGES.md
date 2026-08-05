# Changes

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

