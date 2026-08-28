# Changes

## v0.3.9

### Added

- **Plan forensics export (button + `export_plan_analysis` service)** — every
  optimizer run now snapshots its inputs, planned schedule, and the gate/budget
  decisions to an in-memory record; an **Export Plan Analysis** button (and the
  `battery_optimizer.export_plan_analysis` service) renders that snapshot to
  `battery_optimizer_analysis.md` in the config directory — input prices, solar,
  the SoC input path, the chosen plan (slot/action/buy/sell/SoC/profit), the
  decision trace (floor recovery, SoC cutover, starvation/sell gates), and the
  outcome with a cost breakdown against the baseline and the Emaldo internal
  plan, plus grouped AI-evaluation metrics. Intended for debugging/forensics of
  `battery_optimizer_schedule_chart` decisions; README documents how to collect
  the report. Files: `coordinator.py`, `optimizer.py`, `plan_export.py`,
  `button.py`, `services.py`, `services.yaml`.

### Fixed

- **Export Plan Analysis no longer hides a manual run behind the next skip checkpoint** — the exporter rendered `_last_snapshot`, which is overwritten by every run including periodic skips, so a fresh manual run could be masked by the following checkpoint. A separate `_last_planned_snapshot` is now kept on planned runs only; export prefers it and only falls back to the last snapshot of any kind, warning when the best available run produced no schedule. Files: `coordinator.py`.

## v0.3.8

### Fixed

- **Unreadable battery SoC no longer silently degrades the plan (issue #16)** —
  when the battery SoC read fails or returns `unknown`/`unavailable` at plan
  time, `run_optimizer` previously fell back to `soc_min`, which zeroed the
  discharge budget and pushed an all-idle 24h override straight through the
  day's most profitable window — reported by a user whose battery was actually
  full. Now: (1) the last known good SoC is reused instead, (2) when there is
  no history at all the run is **skipped** (prior schedule kept) with an ERROR
  logged, and (3) the `optimize()` fallback logs at ERROR level if it is ever
  reached by a direct caller. The latent `None SoC × custom rules` crash in
  `_apply_user_mask` is gone with it (a valid SoC is now guaranteed before the
  mask path). Files: `coordinator.py`, `optimizer.py`.

## v0.3.7

### Added

- **Automatic deletion of expired date rules (option `rule_retention_days`)** —
  date-level schedule rules are auto-removed once their `end_date` is older
  than the configured retention: 0 = keep forever (default), 1 = next day,
  7 = after 1 week, 30 = after 30 days. Weekday and default rules are never
  touched. A daily sweep plus a sweep at the start of every optimizer run
  removes rules retroactively the moment the option is enabled; each removed
  rule's `rule_enabled` switch entity disappears with it and the plan
  recomputes. Files: `config_flow.py`, `coordinator.py`, `rules.py`,
  `const.py`, `strings.json`, `translations/*`.

### Changed

- **Disabled rules show "(disabled)" in their title** — the rule subentry
  title now carries an explicit `(disabled)` suffix whenever the rule's
  `enabled` flag is off, both when edited in the UI and when toggled via the
  per-rule switch (the latter now also refreshes the title on toggle).
  Files: `rules.py`, `config_flow.py`, `coordinator.py`.

### Fixed

- **Realized-cost sensors show real values immediately after an HA restart** —
  the persisted 60-day cost-history sidecar was only loaded on the first
  15-minute capture tick, so `realized_cost_today` published `0.0` for up to
  15 minutes after every restart. The sidecar is now restored eagerly at setup.
  Files: `__init__.py`, `coordinator.py`.

- **Rule editor no longer drops the Enabled flag** — `_detail_step` built the
  saved subentry data WITHOUT the `enabled` key, so editing any schedule rule in
  the UI (e.g. unchecking "Enabled") reset the flag to the default True: the
  checkbox reappeared enabled, the per-rule switch stayed on, and the plan kept
  applying the rule. The save dict now includes `enabled` from the rule model,
  so a rule disabled in the editor persists as disabled. The per-rule toggle
  switch already worked (`set_rule_enabled` merges the flag); the editor simply
  never wrote it. File: `config_flow.py`.

## v0.3.6b1

### Fixed

- **Emaldo plan-cost breakdown reports grid cost plus wear (matching the optimizer sensor)** — `emaldo_plan_cost_breakdown`'s docstring previously claimed the `emaldo_plan_cost` sensor state equals `emaldo_grid_cost - emaldo_wear_cost` (minus). The real sensor value is `emaldo_cost = emaldo_grid_cost + emaldo_wear_total` (grid cost plus wear), identical in form to `optimizer_plan_cost` (`baseline_cost - net_profit`). The attribute dict keys were already correct; only the docstring was corrected so the decomposition reads `emaldo_grid_cost + emaldo_wear_cost == sensor state`. No numeric logic or values changed. File: `optimizer.py`.

## v0.3.6

### Added

- **Per-rule Pause/Resume toggle (issue #13, Ask 2)** — each User Schedule rule now
  exposes a `switch.<rule>_rule_enabled` entity that pauses or resumes the rule without
  deleting it. Toggling it persists the `enabled` flag on the rule subentry and immediately
  re-runs the optimizer so the schedule recomputes. Disabled rules are skipped in
  `coordinator._read_user_rules`, so they no longer affect the optimizer plan. The same
  `enabled` flag is also editable as a checkbox inside the rule editor. Files: `rules.py`,
  `coordinator.py`, `switch.py`, `config_flow.py`, `translations/*`.

- **Realized grid cost history + daily sensor (issue #14 follow-up)** — two new sensors
  track the actual money moving across the grid each 15-minute slot, derived from import
  and export energy counters (not the optimizer's plan):
  - `sensor.<entry>_realized_cost_history` — native value is the latest slot's signed net
    cost (negative = refund, so HA's own history graph charts the cost wave); its `slots`
    attribute carries today's per-slot records as JSON for an ApexCharts card, plus
    `today_net` / `today_buy` / `today_sell`.
  - `sensor.<entry>_realized_cost_today` — signed net grid cost realized so far today,
    with `buy_total` / `sell_total` / `import_kwh` / `export_kwh` breakdown.
  A 15-minute `async_track_time_change` task diffs the counters (reset-safe, mirrors the
  solar accuracy sidecar), prices each delta with the cached buy/sell vectors from the
  last optimizer run, and persists a 60-day JSON sidecar
  (`battery_optimizer_cost_history.json`) so the series survives recorder pruning.
  Defaults to **empty = auto-detect** the linked Emaldo unit's
  `grid_import_today` / `grid_export_today` sensors (model-agnostic via the
  entity registry); override with explicit entity IDs or lifetime grid meters
  (e.g. an EM24 `_total` counter) for a cloud-free install. Config fields
  `grid_import_sensor` / `grid_export_sensor` added to setup + options. Files:
  `cost_history.py` (new, pure helper), `const.py`, `coordinator.py`, `sensor.py`,
  `config_flow.py`, `strings.json`, `translations/*`.

### Changed

- **PV Sell Strategy field hidden unless relevant (issue #13, Ask 1)** — in the rule editor
  the `PV Sell Strategy` (`pv_sell`) selector now only appears when the rule action is
  **Charge** or **Discharge**. For Force Idle / Idle Slot Strategy / Optimizer (Control) it
  is correctly hidden, because those actions never sell. No data migration needed —
  `rule_from_data` keeps `pv_sell` defaulting to `inherit`. File: `config_flow.py`.

- **Swedish label for the PV Sell `inherit` option (issue #13)** — the `sv` translation of
  the PV Sell Strategy `inherit` option was `Arv standard`; relabelled to
  `Urladdning av batteri` per tester feedback. File: `translations/sv.json`.

### Fixed

- **SoC forecast (`schedule.soc`) no longer rises during PV Sell windows (issue #14)** —
  the dashboard `soc` trajectory is re-derived in `coordinator._apply_user_rules` via
  `_simulate_soc_trajectory`, which was PV-blind: in its discharge-surplus and idle branches
  it credited *sold* solar to the battery, so `schedule.soc` climbed while those same slots
  were flagged `pv_sell: true`. `_correct_soc_for_pv_sells` (Step 7) fixed the
  optimizer-internal copy, but the re-simulation discarded it. `_simulate_soc_trajectory`
  now takes a `pv_slots` argument and only charges the battery from solar when PV is enabled
  for that slot; sold solar drains by idle loss only. `coordinator._apply_user_rules` now
  threads `masked_pv` through. Regression test: `tests/test_pv_sell_soc.py`. Files:
  `optimizer.py`, `coordinator.py`.

## v0.3.5

### Added

- **Diagnostic sensor for battery wear cost** — new `diagnostic` sensor
  `Battery Wear Cost` (`sensor.battery_optimizer_battery_wear_cost`) exposes
  the configured `battery_wear_cost` (€/kWh cycled, default 0.03) that the
  optimizer charges against every battery cycle when comparing buy vs
  discharge profitability. Reads live from config-entry options, so it tracks
  option changes. Mirrors the existing VAT / transfer-fee / commission
  diagnostic sensors. Files: `sensor.py`, `strings.json`, `translations/en.json`.

### Fixed

- **`user_schedule` chart now renders the baseline timeline when no custom
  rule is set** — when only the default (optimizer) rule exists,
  `coordinator.last_sources is None` and the prior fix returned `schedule: []`,
  which left dashboards showing a blank / "loading…" placeholder. The sensor
  now emits the full 96-slot (today) + 96-slot (tomorrow) schedule with every
  slot sourced `"optimizer"`, so the chart always renders the baseline timeline
  (empty of *user* overrides) instead of a loading placeholder. The state still
  reads `no_schedule` and no slot ever carries `source="user"`, so it cannot be
  mistaken for active user-rule activity. Regression test:
  `tests/test_user_schedule_chart.py`. Refinement of issue #11. File: `sensor.py`.

- **Plan-accuracy sensor no longer drops to `unknown` when the external solar
  sensor is momentarily unavailable** — when `CONF_SOLAR_ACTUAL_SENSOR` is
  configured but the external counter (`solar_ext`) is unreadable,
  `resolve_solar_source` returns `"skip"`. The old code then `return None` for
  the *entire* accuracy record, blanking
  `sensor.battery_optimizer_*_plan_accuracy` (the reported permanent `unknown`).
  The accuracy record is now still written with discharge/charge accuracy; only
  the solar portion is omitted (the solar actuals require their source), keeping
  the auto-tune training data single-sourced while the sensor stays populated
  through solar-sensor outages. Regression test: `tests/test_accuracy_skip.py`.
  Reported in issue #10. File: `coordinator.py`.

## v0.3.4

### Added

- **Diagnostic sensors for cost configuration parameters** — three new
  `diagnostic` sensors expose the optimizer's cost-setup values so they can
  be placed on dashboards or inspected at a glance. They read live from the
  config entry options and reflect option changes:
  - `VAT Multiplier` (`sensor.battery_optimizer_vat_multiplier`) — VAT factor
    applied to import energy cost (dimensionless).
  - `Grid Transfer Fee` (`sensor.battery_optimizer_grid_transfer_fee`) — grid
    transfer fee on imported energy (€/kWh).
  - `Feed-in Sales Commission`
    (`sensor.battery_optimizer_feed_in_sales_commission`) — retailer
    commission on feed-in export (€/kWh).
  File: `sensor.py`, `strings.json`, `translations/en.json`.

### Fixed

- **Upgrade from v0.3.2 → v0.3.3 failed to set up the integration**
  (`AbortFlow: already_configured` in `_ensure_default_rule`) — on upgrade HA
  raised *Flow aborted: already_configured* while adding the default-rule
  subentry, which killed the whole config-entry setup (fresh installs were
  unaffected). Root cause: the default rule was detected by
  `data.level == LEVEL_DEFAULT` but created with `unique_id="default_rule"`;
  when a pre-existing default-rule subentry's stored `level` had drifted (or a
  concurrent setup/reload raced), detection missed it and the re-add collided
  on the unique_id. Detection now also matches `unique_id == "default_rule"`,
  and the `async_add_subentry` call is wrapped so a residual/duplicate
  `already_configured` is logged and tolerated instead of aborting setup. The
  same defensive guard was added to the device-subentry creation. Reported in
  issue #7 (comment 5373140091). File: `__init__.py`.

- **Emaldo entity auto-discovery broken by device_id vs home_id unique_id
  scheme** — `_resolve_emaldo_entity()` built only `{home_id}_{key}`, but
  current ha-emaldo derives entity unique_ids from `device_id`, so every
  lookup returned `None` and SoC / schedule chart / third-party-PV switch fell
  back to failure ("Cannot optimize"). The resolver now tries the `device_id`
  base first, then `home_id`, so auto-discovery works across ha-emaldo
  versions (issue #9). File: `coordinator.py`.

- **PV Sell Strategy never triggered on high-solar days** — the economic gate
  in `_plan_pv_sell_slots()` only opened when a stored solar kWh displaced a
  future grid buy (a planned discharge slot). On sunny days the battery fills
  entirely from free solar, so no discharge is planned and the gate fell back
  to a same-slot `sell_price > buy_price` check — which can never open because
  buy always includes VAT + transfer fees. Result: PV export never happened and
  the "sell morning solar, charge from later solar" feature was dead precisely
  on the days it targets. The gate now also models the alternative use of
  stored solar on no-discharge days: it displaces *later solar export*, so a
  slot sells whenever its sell price beats the cheapest post-cutover sell price
  (× `pv_sell_margin`). This captures the intraday morning-vs-midday spread, is
  wear-free (direct export), and preserves the existing SoC-starvation and floor
  guards.   Reported in issue #8. File: `optimizer.py`.

### Changed

- **Schedule chart no longer shows the misleading `128` (`SLOT_NO_OVERRIDE`)
  value for already-elapsed slots** — the diagnostic `schedule_chart` sensor
  renders the full 96-slot plan, and slots before `start_slot` (today's
  already-run periods) kept the default override byte `128` while the battery
  had in fact moved on. The sensor now reports `value: null` and a new
  `past: true` flag for those slots; future idle slots still report `0` (real
  `SLOT_IDLE`). Purely cosmetic — the device push path already forced idle
  (`0`) for elapsed slots, so battery behaviour is unchanged. Secondary
  symptom of issue #8. File: `sensor.py`.

## v0.3.3

### Fixed

- **Emaldo plan-cost estimate over-charged from grid during daytime solar** —
  the Rest-of-Day/tomorrow Emaldo cost benchmark simulated the internal AI
  plan with charge-mode slots buying the full charge energy from the grid and
  silently dropping solar surplus (discharge-mode surplus never exported,
  idle-mode imported the full base load even under solar). On solar days this
  inflated the benchmark by €1.5+ (live: 2.76 € vs the 0.33 € optimizer plan
  while the battery in fact fills from surplus PV for free). The simulation
  now honours the real device semantics: with third-party PV enabled, surplus
  solar charges the battery in EVERY mode (grid only tops up in charge-mode
  slots); with PV disabled external solar is exported and the house draws the
  full base load. The effective per-slot PV switch state is the optimizer's
  own planned PV sell/charge schedule (`_plan_pv_sell_slots`), computed
  before the benchmark instead of after. Full-battery charge slots no longer
  import and export the surplus instead. Files: `optimizer.py` (emaldo sim
  rewrite + PV-precompute reorder), `tests/test_cost_breakdown.py` (2 new
  PV-aware emaldo tests, stale zero-result assertions updated).

- **`sensor.solar_balance` rejected by HA (device_class/state_class clash)** —
  the Solar Balance sensor set `device_class=energy` together with
  `state_class=measurement`. HA forbids the `energy` device class with a
  non-cumulative state class, so the entity failed to set up with
  *"using state class 'measurement' which is impossible considering device
  class ('energy')"*. The value is average daily solar (kWh/day), a rate, not
  a cumulative total — so the device class was removed; unit (`kWh`) and
  `measurement` state class are kept. Reported in issue #7.

- **Schedule weekday selector showed numbers, not names** — the custom
  schedule rule form (`RuleSubentryFlow`) rendered the weekday picker as
  numeric chips `0 ×`…`6 ×` (Python `weekday()`: 0=Mon…6=Sun). The selector
  already carried `translation_key="weekday"`, but HA only localizes that key
  when the option values are its canonical weekday keys. Options are now
  `monday`…`sunday`; HA's built-in `weekday` frontend translations render
  them as localized day names (e.g. Swedish "måndag"), and the selected keys
  are mapped back to `int` (0=Mon…6=Sun) for storage. Covers the request in
  issue #4 (comment 5360285748). File: `config_flow.py`.

## v0.3.2

### Changed

- **Night-pool reservation on COMBINED days (bias-based, safe default)** — on
  no/partial-solar days the single discharge pool covers evening → morning →
  night LAST, so the cheapest pre-solar night grid buys starve even when the
  battery holds plenty of stored energy. A configurable reservation bias
  (`_NIGHT_RESERVE_BIAS`, hardcoded default **0.6**, module constant in
  `optimizer.py`) now reserves a slice of the initial battery pool for the
  cheapest pre-solar night slots **before** the combined buy-desc pass runs:
  `night_reserve_kwh = initial_usable × min(1, bias × refill_fraction / 0.95)`
  scaled by how much solar refill actually happened (bias 1.0 + full refill
  reserves the whole initial pool). Reserved slots are assigned cheapest-first
  (ascending buy, latest slot on ties) — the exact slots the combined pool
  starves — and the combined loop skips them and allocates the remaining
  budget buy-desc as before. The over-commit correction (step 5a) remains the
  floor safety net; reserved energy is not subtracted from the combined budget
  (when the trajectory violates the floor, the correction trims the cheapest
  committed slot — the reserved tail first — so the reservation is never
  doubled into the budget).
  Bias = 0 disables the reservation entirely (byte-identical legacy plan);
  SPLIT-mode days (`refill_fraction ≥ 0.95`) are untouched. Live-day
  verification: bias 0.6 → 2.8337 €/day, 20 night discharges, min SoC 27.7 %
  (was 2.3743 / 6 / 52.4 %), battery no longer forced to 100 % by 16:00;
  bias 0 → exact legacy baseline. A/B sweep: 0.6 → 2.8337, 0.8 → 2.9819,
  plateau 2.9822, never violates the 20 % floor. Tuning for later: raise/lower
  the single module constant; bias ≥ 1.0 restores full pre-reservation
  behavior for aggressive day-priority.

- **Durable no-refill solar regime raises the Case A discharge floor** — in
  winter / snow-on-panels weeks the battery cannot be refilled by solar, so
  each stored kWh discharged today must beat the cheapest KNOWN future grid
  recharge through the round-trip: `floor = min(remaining today, tomorrow) /
  round_trip_factor + wear`. The regime is a per-day EWMA (α 0.1) over the
  scaled-forecast solar fraction of the user's usable band
  `(soc_max − soc_min) × capacity`, persisted to
  `battery_optimizer_solar_regime.json`; gate engages only after 3 consecutive
  low days below 0.25 and disengages after 3 high days above 0.40 (hysteresis
  dead-zone, once-per-day update, cold start OFF, seeded 1.0). Relative to the
  user's own battery → generic across installs. `solar_full_recharge` (today's
  fact) overrides the trend and restores the wear floor. Legacy path
  (regime off) byte-identical; regime on blocks sub-round-trip discharge
  (snow scenario: 13D 0.3233 → 0D, energy held at 44 % end SoC; cheap night
  refill 0.01 still discharges the 0.12 peak). Files: `solar_regime.py`,
  `optimizer.py` (Case A floor + probe threading), `coordinator.py` (state
  load/save + tomorrow-price ordering for the gate).

### Added

- **Solar Balance sensor + 3rd-party PV production docs** —
  `sensor.battery_optimizer_solar_balance` reports average daily solar
  production (kWh, trailing 7 days) derived from the persisted accuracy
  records (per-run external-counter deltas summed per calendar date;
  `unknown` before 5 sampled days). Attributes: `daily_base_load_kwh`
  (auto base load × 24), `self_sufficiency` (< 1 = net importer),
  `battery_days` (usable band ÷ daily base load), `usable_band_kwh`,
  `days_sampled`, `window_start`/`window_end`, `solar_source`. Display-only —
  never gates planning (solar regime + Case B arbitrage stay decision makers).
  README: new *Solar Production Sensor (3rd-party inverter)* section
  documenting the cumulative-counter contract and which Solis sensors fit
  (Total Energy best; Energy Today works via built-in `reset_crossed`
  handling; Active Power not accepted). Files: `solar_balance.py` (pure),
  `sensor.py`, `tests/test_solar_balance.py`, README.

### Fixed

- **Solar Regime sensor attributes never emitted** — `sensor.py` carried an
  orphan duplicate `extra_state_attributes` inside `SolarRegimeSensor`
  (second definition wins in Python), so the regime attributes
  (`ewma`, `forecast_fraction`, `low_days`, `high_days`, thresholds…) were
  dead and the `plan_accuracy` dict leaked onto the regime sensor instead.
  Moved the property to `PlanAccuracySensor`; regime attributes now emit.

- **Morning low-SoC plan emptied of ALL discharge (0C 0D 65I)** — when the
  battery starts at/below the floor target (e.g. 15 % = `soc_min` at 07:45),
  the discharge over-commit correction checked `min(traj_a)` over the WHOLE
  96-slot trajectory against `floor_target_kwh`. The start dip (2.25 kWh <
  3.0 kWh floor) is permanent — dropping discharge cannot raise it — so the
  loop dropped every committed discharge slot and emitted an idle-only plan
  despite profitable peak prices (slot 79 = 0.2869 €/kWh) and solar refill to
  ~69.7 %. Live: 1.2789 €/day vs 2.3972 baseline. The floor check now applies
  only to the discharge-affected region `min(traj_a[first_dis:])` (from the
  first discharge slot onward); a pre-first-discharge dip is plan-time state
  (battery parked at floor, solar refills it), not discharge over-commit.
  Pre-solar overnight discharge with the battery at the floor is still trimmed
  correctly (first_dis lands in the night). Start-above-floor days are
  byte-identical. Verification: morning repro (start_slot 31, initial 15 %)
  29 discharge slots, 1.7686 €/day (was 0D, 0.9363); legacy bias=0 baseline
  unchanged (2.3743 / 6 / 52.4 %); RC6 case unchanged (2.8337 / 20 / 27.7 %).

## v0.3.1

### Fixed

- **Baseline cost understated/negative on sunny days** — the no-battery
  baseline was computed from the net load (`max(base_load − solar, 0)`), so
  on solar-surplus days it counted little or no grid import and could go
  negative (export revenue exceeding the tiny import cost). The baseline now
  represents the true no-battery cost: the grid imports the **full base load**
  at the buy price in every slot (`base_load_kw × slot_duration`) and solar
  surplus above base load is exported at the sell price. Always positive on
  net; savings (`baseline − actual`) now reflect genuine avoided purchases.
  The Emaldo idle benchmark used the same net-load formula and is corrected
  identically (idle = full base-load import + surplus export).

### Changed

- **Full fee decomposition on the cost sensors** — all four plan-cost
  sensors and both baseline-cost sensors now expose the components of their
  state value as attributes, in addition to the existing `grid_cost` /
  `wear_cost` / `cycled_kwh` (optimizer) and `emaldo_grid_cost` /
  `emaldo_wear_cost` / `emaldo_cycled_kwh` (Emaldo) keys. The Finnish
  household price model is decomposed per kWh: buy = spot × VAT + transfer +
  commission, sell = spot − commission; import VAT is zero when the spot
  price is negative (no export tax, no export transfer). Import kWh split
  into `_energy`, `_transfer`, `_tax`, `_commission`; exports into
  `_export_energy`, `_export_commission`. The decomposition sum matches the
  state value (component sums equal `baseline_cost`,
  `baseline_cost − net_profit`, and `emaldo_cost` respectively).
  State values are unchanged — purely additive.
- **Translatable selector options in the rule subentry flow** — the PV-sell
  dropdown (`inherit` / `sell` / `charge`) and the weekday multi-select
  previously showed raw option values (English strings). Both now use
  `SelectSelector` with a `translation_key`, so the labels resolve through
  the locale files (`selector.pv_sell.options`, `selector.weekday.options`)
  in English, Danish, Finnish, Norwegian Bokmål and Swedish.

## v0.3.0

### Added

- **User Schedule Layer** — persistent schedule rules (config
  subentries) that select, per time window, between the optimizer plan,
  the battery's internal AI, and manual charge/idle/discharge actions,
  with per-rule PV sell behavior. Precedence: date > weekday > default;
  same-level overlaps rejected. New `battery_optimizer_user_schedule_chart`
  diagnostic sensor exposes the user plan for dashboards. The sensor now
  spans 48 h (today + tomorrow) so the User Schedule chart overlays the
  other schedule charts on the same time axis.
- **Localization** — the config, options and schedule-rule subentry flows
  plus all entity names (sensors, buttons, switches) are localized in
  English, Danish, Finnish, Norwegian Bokmål and Swedish
  (`strings.json` + `translations/*.json`).

## v0.2.4

### Added

- **Solar forecast scale (over-forecast compensation)** — new config option
  `solar_forecast_scale` (default `0` = auto). The whole-day solar forecast is
  multiplied by the scale at the `_get_solcast_forecast()` choke point (applies
  to both today and tomorrow planning). In auto mode the scale is tuned per run
  from the persisted accuracy history: an EWMA (alpha 0.1, seed 1.0) over each
  record's *raw-basis* ratio `(actual / planned) × scale_used`, which recovers
  the true unscaled bias even when the plan ran under a different scale (fixes
  the naive scaled-basis feedback that would otherwise sit at ratio 1.0 and
  never correct). Output and per-sample ratios are clipped to 0.3–1.2. Records
  with a PV daily-counter reset crossing, fewer than 8 elapsed slots, planned
  solar under 0.1 kWh, or non-numeric fields are excluded; 5 valid records are
  required before tuning engages. Manual values (0.3–1.2) disable the auto-tune.
- Accuracy sidecar records now persist `solar_scale_used` and
  `solar_reset_crossed`; `solar_scale_used` is attributed to the run that
  actually produced the measured slots (retained at plan time, read at the next
  accuracy comparison). Legacy records without the field default to scale 1.0.
- **External actual-solar sensor** — new optional config option
  `solar_actual_sensor` (default empty). Plan-accuracy and the solar-scale
  auto-tune measure actual solar from this cumulative energy counter (Wh/kWh,
  normalized) instead of the Emaldo-internal balance-derived estimate, which
  household loads distort. When empty, behavior is unchanged (Emaldo
  fallback). If the configured counter is unavailable at an accuracy run, that
  record is skipped with a warning — tuning data never mixes sources. Records
  carry `solar_source` (`external`/`emaldo`).
- Pure-module unit tests (`tests/test_solar_scale.py`, 16 tests) — warmup,
  EWMA math, raw-basis normalization, fixed-point convergence, clamps, and
  record filters. 35 tests total.
- Pure-module unit tests for the external-solar helpers
  (`tests/test_solar_actual.py`, 16 tests) — normalization, counter-delta
  reset handling, source resolution. 51 tests total.

### Changed

- Plan Accuracy no longer purely observational: auto-tune (default) feeds the
  accuracy history back into solar planning.
- **Replan triggers — idle-gap gate + SoC-divergence watcher** — conditional
  re-runs now catch two cases the 10 % deviation check missed. The **idle-gap
  gate** (`_should_reoptimize`, throttled 30 min) forces a replan when the
  plan leaves the current slot idle while the grid buys above battery wear
  cost and the battery has headroom: re-running with the actual (often
  higher) SoC lets the discharge allocation open earlier slots on low-load
  days. The **divergence watcher** (`_on_soc_state_change`, throttled 15 min,
  runs independently of `enable_soc_safeguard`) forces a replan when actual
  SoC deviates > 5 % from the plan's projected slot SoC, catching forecast
  error / unexpected loads / cheap-day under-discharge between the polled
  checkpoints. Thresholds and throttles configurable via constants in
  `const.py`.
- **Plan-cost subcost attributes** — the four plan-cost sensors
  (`optimizer_plan_cost`, `tomorrow_optimizer_plan_cost`,
  `emaldo_plan_cost`, `tomorrow_emaldo_plan_cost`) now expose the components
  of their main value as attributes. The optimizer cost sensors report
  `grid_cost`, `wear_cost`, `cycled_kwh` (`grid_cost + wear_cost == state`);
  the Emaldo cost sensors report `emaldo_grid_cost`, `emaldo_wear_cost`,
  `emaldo_cycled_kwh` (`emaldo_grid_cost - emaldo_wear_cost == state`).
  State values are unchanged — purely additive.

### Fixed

- **PV-sell economic gate compared against the wrong price** — a slot was
  marked sell whenever `sell_price > buy_price` at that slot. On a Finnish
  tariff (sell 0.002–0.003 vs buy 0.0839+) that gate fired for morning
  exports that lose money versus storing: a stored kWh displaces a future
  grid buy through the round-trip. The gate now compares the sell price
  against the **stored value** of that kWh — the cheapest future discharge
  buy price, discounted by round-trip efficiency (`η_c × η_d`) and the new
  `pv_sell_margin` config (default `1.0`). The `pv_sell_min_price_spread`
  floor still applies. Live day: old code sold all 6 slots to noon (profit
  €0), gate now sells none; sell slots appear only when the export price
  actually beats storage.
- **Discharge-assigned surplus slots understated solar absorption** — the
  grid-charge balance only credited solar on idle surplus slots. Discharge
  slots on surplus solar also absorb it (Emaldo firmware charges from excess
  solar even in discharge mode), so the plan bought grid charge it never
  needed. Surplus absorption on discharge slots is now credited, scaled by
  the new `solar_forecast_margin` config (default `0.85`) and capped by the
  remaining headroom — conservative direction, so an over-forecast never
  leaves the plan grid-charge-starved.

### Added

- **Wear-netted savings reporting** — the result now carries `cycled_kwh`
  (battery-internal kWh discharged), `wear_cost_total`
  (`wear_cost_per_kwh × cycled_kwh`) and `net_profit` (`total_profit −
  wear_cost_total`). Estimated-savings sensors report `net_profit` (honest
  headline: gross minus degradation cost) and expose `gross_savings`,
  `wear_cost` and `cycled_kwh` attributes; both optimizer plan-cost sensors
  report `baseline_cost − net_profit`. The Emaldo benchmark cost is netted
  for its own cycles so the comparison is like-for-like.
- **PV-sell stored-value gate config** — `pv_sell_margin` (default `1.0`),
  multiplier on the stored-value sell threshold.
- **Discharge-surplus absorption config** — `solar_forecast_margin`
  (default `0.85`), fraction of discharge-slot surplus solar credited to the
  grid-charge balance.
- Pure-module unit tests (`tests/test_mitigations.py`, 11 tests) — wear
  netting (3), PV-sell stored-value gate (3), over-commit floor hold (2),
  discharge-surplus absorption (3). 82 tests total.

## v0.2.3

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

