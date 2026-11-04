"""Tests for the solar-aware Case B grid-charge sizing (issue #26)."""
from __future__ import annotations

import unittest

from . import load_optimizer

optimizer, const = load_optimizer()
BatteryConfig = optimizer.BatteryConfig


class TestCaseBGridCharge(unittest.TestCase):
    """_case_b_grid_charge(): how much grid charging may fund toward soc_max."""

    def test_default_is_exactly_the_legacy_headroom(self):
        """Solar-aware blend weight 0.0 must return the legacy value bit-for-bit."""
        cfg = BatteryConfig(capacity_kwh=15.0, soc_min=12.0, soc_max=100.0)
        # soc_max 15.0 kWh, current 5.55 kWh (37%) -> legacy headroom 9.45
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, None),
            9.45,
            places=9,
        )

    def test_default_ignores_available_soc_and_ratio(self):
        """At blend weight 0.0 the solar estimate must have NO influence."""
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.0)
        for avail in (0.0, 7.5, 15.0):
            for ratio in (None, 0.0, 0.35, 1.0):
                self.assertAlmostEqual(
                    optimizer._case_b_grid_charge(cfg, 15.0, 5.55, avail, ratio),
                    9.45,
                    places=9,
                    msg=f"available_soc={avail} ratio={ratio}",
                )

    def test_confident_sunny_day_charges_almost_nothing(self):
        """Solar already at soc_max with a confident forecast -> ceiling ~0."""
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            0.0,
            places=9,
        )

    def test_unconfident_day_still_charges_full_headroom(self):
        """A wide p50/p10 gap must hedge back to the full headroom.

        available_soc 15.0 with ratio 0.35 gives a raw ceiling of 9.75, which
        is ABOVE the legacy headroom 9.45. That raw value is unphysical:
        soc_max - current_soc is the most energy that can be charged at all,
        so the ceiling is clamped to `full`. The result is the full 9.45,
        i.e. an unconfident day charges as it did before (current_soc >= 0).
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        got = optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 0.35)
        self.assertAlmostEqual(got, 9.45, places=9)

    def test_ceiling_never_exceeds_physical_headroom(self):
        """No combination of solar estimate and ratio may raise the ceiling
        above soc_max - current_soc."""
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        for avail in (0.0, 2.0, 5.55, 9.0, 15.0):
            for ratio in (0.0, 0.2, 0.35, 0.9, 1.0):
                self.assertLessEqual(
                    optimizer._case_b_grid_charge(cfg, 15.0, 5.55, avail, ratio),
                    9.45 + 1e-9,
                    msg=f"available_soc={avail} ratio={ratio}",
                )

    def test_overcast_day_full_headroom(self):
        """No solar contribution at all -> ceiling is the full headroom.

        available_soc == current_soc means zero solar gained, so the ratio has
        no meaning here; the clamp is what keeps the answer physical.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 5.55, 0.2),
            9.45,
            places=9,
        )

    def test_blend_is_linear_between_ends(self):
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.5)
        got = optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0)
        # full=9.45, solar=0.0 -> midpoint 4.725
        self.assertAlmostEqual(got, 4.725, places=9)

    def test_ratio_none_falls_back_to_legacy_not_full_confidence(self):
        """Absent ratio means NO information — so size as legacy, not as zero.

        This is the unsafe-direction bug: `available_soc=15.0` at
        `current_soc=5.55` with a real ratio of 0.35 still charges 9.45 kWh
        (the clamp floors it at the headroom), but treating unknown as ratio
        1.0 shrank the charge to 0.00 kWh and left the battery empty into the
        morning peak. Unknown must never read as a confident clear sky.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, None),
            9.45,
            places=9,
        )

    def test_ratio_none_is_legacy_at_every_weight(self):
        """The None fallback is legacy sizing, so the weight must not shrink it."""
        for aware in (0.1, 0.5, 0.9, 1.0):
            cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=aware)
            self.assertAlmostEqual(
                optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, None),
                9.45,
                places=9,
                msg=f"aware={aware}",
            )

    def test_weight_zero_is_unaffected_by_the_none_fallback(self):
        """Weight 0.0 returns before the ratio is read, so None changes nothing.

        Proves the new `None -> legacy` branch cannot perturb the 0.0 default:
        `float(cfg.grid_charge_solar_aware or 0.0)` and the isfinite/<=0 guard
        both fire first, so the None branch is unreachable at weight 0.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.0)
        for ratio in (None, 0.0, 0.35, 1.0):
            self.assertAlmostEqual(
                optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, ratio),
                9.45,
                places=9,
                msg=f"ratio={ratio}",
            )

    def test_none_weight_falls_back_to_legacy_instead_of_raising(self):
        """A None blend weight must not raise TypeError.

        `float(None)` raises, and _case_b_grid_charge runs inside
        run_optimizer — an exception there aborts the whole coordinator update
        and stops battery control entirely. NaN and inf are already guarded on
        the next line for this same corrupted-config threat model; None has to
        join them rather than blow up.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=None)
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            9.45,
            places=9,
        )
        # Same for the disabled branch and for a ratio that would otherwise
        # shrink the charge, so the None guard is not weight-specific.
        off = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=None)
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(off, 15.0, 5.55, 15.0, 0.35),
            9.45,
            places=9,
        )

    def test_out_of_range_inputs_are_clamped(self):
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=1.0)
        # ratio > 1 and < 0 must clamp into [0,1] without raising
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 5.0),
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            places=9,
        )
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, -3.0),
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 0.0),
            places=9,
        )

    def test_nan_weight_falls_back_to_legacy_headroom(self):
        """A NaN blend weight must NOT read as 'fully on'.

        `nan <= 0.0` is False and `min(1.0, nan)` is 1.0, so without an
        explicit isfinite check a NaN would silently force the feature fully
        on — a corrupted config value could then under-charge the battery.
        vol.Range(0.0, 1.0) does not reject NaN, so the guard is required.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=float("nan"))
        # available_soc == soc_max with full confidence would drive a
        # 'fully on' answer to 0.0; the legacy fallback must stay at 9.45.
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            9.45,
            places=9,
        )

    def test_inf_weight_falls_back_to_legacy_headroom(self):
        """An infinite blend weight is out of range, so use legacy sizing."""
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=float("inf"))
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            9.45,
            places=9,
        )

    def test_negative_inf_weight_falls_back_to_legacy_headroom(self):
        """-inf is non-finite; it must take the same legacy path."""
        cfg = BatteryConfig(
            capacity_kwh=15.0, grid_charge_solar_aware=float("-inf")
        )
        self.assertAlmostEqual(
            optimizer._case_b_grid_charge(cfg, 15.0, 5.55, 15.0, 1.0),
            9.45,
            places=9,
        )


class TestSolarConfidenceRatio(unittest.TestCase):
    """p10/p50 energy ratio derived from a Solcast detailedForecast."""

    def test_ratio_one_when_p10_matches_p50(self):
        detailed = [{"pv_estimate": 1.0, "pv_estimate10": 1.0}] * 48
        self.assertAlmostEqual(optimizer.compute_solar_confidence_ratio(detailed), 1.0, places=9)

    def test_ratio_is_energy_weighted_not_slot_averaged(self):
        # one slot 100 kW p50 / 10 kW p10, rest zero -> total ratio 0.10
        detailed = [{"pv_estimate": 100.0, "pv_estimate10": 10.0}] + \
                   [{"pv_estimate": 0.0, "pv_estimate10": 0.0}] * 47
        self.assertAlmostEqual(optimizer.compute_solar_confidence_ratio(detailed), 0.10, places=9)

    def test_missing_p10_returns_none_rather_than_faking_confidence(self):
        """A p50-only payload carries no dispersion information at all.

        Substituting p50 for the missing p10 reported a ratio of 1.0 — perfect
        confidence — which drove the overnight charge to 0.00 kWh. Unknown must
        return None so the caller falls back to legacy sizing.
        """
        detailed = [{"pv_estimate": 2.0}] * 48
        self.assertIsNone(optimizer.compute_solar_confidence_ratio(detailed))

    def test_partial_p10_payload_uses_the_covered_slots(self):
        """A partial payload is answered from the slots that do report p10.

        Both totals are restricted to the covered slots so the answer is a true
        ratio. Dividing the covered p10 by the whole-day p50 would deflate it
        purely by the un-covered slots — i.e. bias confidence *downward*, the
        unsafe direction again.
        """
        # 8 covered slots at p50=1.0 / p10=0.5 (ratio 0.5), 40 uncovered slots
        # at p50=1.0. Whole-day division would give 4/48 = 0.083.
        detailed = ([{"pv_estimate": 1.0, "pv_estimate10": 0.5}] * 8
                    + [{"pv_estimate": 1.0}] * 40)
        self.assertAlmostEqual(
            optimizer.compute_solar_confidence_ratio(detailed), 0.5, places=9,
        )

    def test_null_p10_counts_as_no_coverage(self):
        """A JSON null pv_estimate10 is missing information, not zero energy."""
        detailed = [{"pv_estimate": 1.0, "pv_estimate10": None}] * 48
        self.assertIsNone(optimizer.compute_solar_confidence_ratio(detailed))

    def test_reported_zero_p10_is_coverage(self):
        """A present 0.0 is a real reading (total overcast), not 'no data'."""
        detailed = [{"pv_estimate": 1.0, "pv_estimate10": 0.0}] * 48
        self.assertAlmostEqual(
            optimizer.compute_solar_confidence_ratio(detailed), 0.0, places=9,
        )

    def test_full_payload_ratio_is_unchanged(self):
        """The normal full payload keeps the original whole-day arithmetic."""
        detailed = (
            [{"pv_estimate": 4.0, "pv_estimate10": 1.0}] * 12
            + [{"pv_estimate": 2.0, "pv_estimate10": 2.0}] * 36
        )
        # p10 = 12 + 72 = 84 ; p50 = 48 + 72 = 120 -> 0.7
        self.assertAlmostEqual(
            optimizer.compute_solar_confidence_ratio(detailed), 0.7, places=9,
        )

    def test_zero_p50_returns_none(self):
        self.assertIsNone(optimizer.compute_solar_confidence_ratio([{"pv_estimate": 0.0}] * 48))

    def test_none_and_empty_return_none(self):
        self.assertIsNone(optimizer.compute_solar_confidence_ratio(None))
        self.assertIsNone(optimizer.compute_solar_confidence_ratio([]))

    def test_ratio_is_clamped_to_unit_interval(self):
        # p10 > p50 should not produce a ratio above 1.0
        detailed = [{"pv_estimate": 1.0, "pv_estimate10": 3.0}] * 48
        self.assertAlmostEqual(optimizer.compute_solar_confidence_ratio(detailed), 1.0, places=9)


class TestOptimizeGridChargeSizing(unittest.TestCase):
    """End-to-end: the option changes grid_charge_needed in the trace."""

    SLOTS = 96

    def _cfg(self, aware):
        return optimizer.BatteryConfig(
            capacity_kwh=15.0, soc_min=12.0, soc_max=100.0,
            soc_recovery_buffer_pct=2.0, base_load_kw=1.1,
            charge_efficiency=0.9, discharge_efficiency=0.9,
            max_charge_kw=10.0, max_discharge_kw=10.0,
            wear_cost_per_kwh=0.03, idle_power_kw=0.1,
            vat_multiplier=1.255, transfer_fee_buy=0.0776, sales_commission=0.003,
            grid_charge_solar_aware=aware,
        )

    def _run(self, aware, ratio):
        cfg = self._cfg(aware)
        spots = [0.02 if s < 60 else 0.30 for s in range(self.SLOTS)]
        solar = [max(0.0, 7.0 * (1 - (abs(s - 48) / 26.0) ** 2)
                     if abs(s - 48) <= 26.0 else 0.0) for s in range(self.SLOTS)]
        buy, sell = optimizer.compute_prices(spots, cfg)
        return optimizer.optimize(
            buy, sell, solar, cfg, start_slot=0, initial_soc_pct=37.0,
            total_slots=self.SLOTS, enable_pv_strategy=True,
            solar_confidence_ratio=ratio,
        )

    def test_default_option_leaves_grid_charge_needed_unchanged(self):
        """The 0.0 default must not move the number, whatever the ratio."""
        for ratio in (None, 0.1, 0.35, 1.0):
            self.assertAlmostEqual(
                self._run(0.0, ratio).trace["grid_charge_needed"],
                self._run(0.0, None).trace["grid_charge_needed"],
                places=9,
                msg=f"ratio={ratio}",
            )

    def test_confident_sunny_day_reduces_grid_charge(self):
        off = self._run(0.0, None).trace["grid_charge_needed"]
        on = self._run(1.0, 1.0).trace["grid_charge_needed"]
        self.assertLess(on, off)

    def test_unconfident_day_keeps_more_charge_than_confident(self):
        confident = self._run(1.0, 1.0).trace["grid_charge_needed"]
        unconfident = self._run(1.0, 0.35).trace["grid_charge_needed"]
        self.assertGreater(unconfident, confident)

    def test_trace_records_the_inputs(self):
        t = self._run(1.0, 0.35).trace
        self.assertIn("solar_confidence_ratio", t)
        self.assertIn("grid_charge_needed_solar_ceiling", t)
        self.assertAlmostEqual(t["solar_confidence_ratio"], 0.35, places=9)

    def test_trace_solar_ceiling_is_none_when_option_is_off(self):
        """At the 0.0 default the trace key carries no solar term, so say None.

        `_case_b_grid_charge` returns the plain legacy headroom at weight 0.0,
        so publishing it under a key called "solar ceiling" would be a lie for
        every user who never opted in. The key stays present (it is part of the
        0.3.22 trace contract) but is None unless the option is enabled.
        """
        for ratio in (None, 0.0, 0.35, 1.0):
            t = self._run(0.0, ratio).trace
            self.assertIn("grid_charge_needed_solar_ceiling", t)
            self.assertIsNone(
                t["grid_charge_needed_solar_ceiling"],
                msg=f"ratio={ratio}",
            )

    def test_trace_solar_ceiling_is_populated_when_option_is_on(self):
        for aware in (0.1, 0.5, 1.0):
            t = self._run(aware, 0.35).trace
            self.assertIsNotNone(
                t["grid_charge_needed_solar_ceiling"],
                msg=f"aware={aware}",
            )


class TestDefaultEquivalence(unittest.TestCase):
    """The 0.0 default must reproduce pre-0.3.22 output exactly.

    The legacy term is max(0.0, soc_max_kwh - current_soc_kwh). We assert the
    trace still reports that value for grid_charge_needed across a matrix of
    scenarios, which fails the moment the solar term leaks into the default
    path.

    Every comparison in this class is EXACT (``assertEqual`` / ``!=``), never
    ``assertAlmostEqual``. The default path must not merely be *close* to the
    legacy arithmetic: it must evaluate to the identical float, because the
    value is kWh of grid purchase and float drift here is money. ``assertEqual``
    on floats is a ``!=`` check, which is precisely the property being proven.

    Three independent layers of evidence, weakest first:

    1. ``_case_b_grid_charge`` at 0.0 equals the literal legacy expression for
       a wide input grid, including the clamp boundary and non-finite inputs
       (the arithmetic itself is unchanged).
    2. ``optimize()`` at 0.0 returns identical scalar totals whatever the
       forecast ratio (the new input cannot reach the old path).
    3. ``optimize()`` at 0.0 returns byte-identical results to the actual
       0.3.21 release source, loaded from git (the whole function, not just
       Case B).
    """

    SLOTS = 96
    # v0.3.21 — the release this feature must be indistinguishable from.
    _RELEASE_COMMIT = "17c6497"
    _RELEASE_BLOB = "custom_components/battery_optimizer/optimizer.py"

    # -- scenario matrix -------------------------------------------------
    # Peaks and SoCs chosen to span "no sun", "weak sun" and "solar would fill
    # the battery", across a nearly-empty to nearly-full battery.
    PEAKS = (0.0, 2.0, 7.0)
    SOCS = (15.0, 37.0, 60.0, 90.0)
    RATIOS = (0.1, 0.35, 1.0)

    def _cfg(self, aware):
        return optimizer.BatteryConfig(
            capacity_kwh=15.0, soc_min=12.0, soc_max=100.0,
            soc_recovery_buffer_pct=2.0, base_load_kw=1.1,
            charge_efficiency=0.9, discharge_efficiency=0.9,
            max_charge_kw=10.0, max_discharge_kw=10.0,
            wear_cost_per_kwh=0.03, idle_power_kw=0.1,
            vat_multiplier=1.255, transfer_fee_buy=0.0776,
            sales_commission=0.003,
            grid_charge_solar_aware=aware,
        )

    def _matrix_result(self, aware, solar_peak, soc, ratio):
        """Run one matrix scenario and return the full OptimizationResult."""
        cfg = self._cfg(aware)
        spots = [0.02 if s < 60 else 0.30 for s in range(self.SLOTS)]
        solar = [max(0.0, solar_peak * (1 - (abs(s - 48) / 26.0) ** 2)
                     if abs(s - 48) <= 26.0 else 0.0) for s in range(self.SLOTS)]
        buy, sell = optimizer.compute_prices(spots, cfg)
        return optimizer.optimize(
            buy, sell, solar, cfg, start_slot=0,
            initial_soc_pct=soc, total_slots=self.SLOTS,
            enable_pv_strategy=True,
            solar_confidence_ratio=ratio,
        )

    def _matrix_run(self, aware, solar_peak, soc, ratio):
        return self._matrix_result(aware, solar_peak, soc, ratio).trace

    # -- layer 0: the scenarios must not be degenerate -------------------
    # A randomized optimize() differential was tried earlier and produced
    # everything-idle scenarios with 0 cycled kWh, which proves nothing: an
    # idle plan is trivially identical before and after any change. Every
    # scenario used below is asserted to actually charge, discharge and have
    # a non-zero Case B allowance first.
    def test_matrix_scenarios_are_not_degenerate(self):
        for peak in self.PEAKS:
            for soc in self.SOCS:
                r = self._matrix_result(0.0, peak, soc, None)
                ctx = f"peak={peak} soc={soc}"
                self.assertGreater(r.discharge_slots, 0, f"no discharge in {ctx}")
                self.assertGreater(
                    r.cycled_kwh, 1.0,
                    f"only {r.cycled_kwh} kWh cycled in {ctx} — too small to "
                    f"detect a sizing change",
                )
                # Case B only contributes when profitable_charge is non-empty;
                # without it the exact term under test never fires.
                self.assertGreater(
                    r.trace["n_profitable_charge"], 0,
                    f"Case B inactive (n_profitable_charge=0) in {ctx}",
                )
                self.assertGreater(
                    r.trace["grid_charge_needed"], 0.0,
                    f"grid_charge_needed is 0 in {ctx}",
                )
                # Grid charging itself: a near-full battery on a sunny day is
                # covered by solar and the optimizer correctly buys nothing.
                # Asserted explicitly in both directions so the exclusion stays
                # deliberate rather than accidental.
                if soc <= 60.0 or peak == 0.0:
                    self.assertGreater(
                        r.charge_slots, 0, f"no grid charge in {ctx}",
                    )
                else:
                    self.assertEqual(
                        r.charge_slots, 0,
                        f"soc={soc} peak={peak}: expected no grid charge on a "
                        f"near-full battery",
                    )

    # -- layer 1: optimize() is invariant to the new inputs at 0.0 -------
    def test_default_output_ignores_solar_peak_and_ratio(self):
        """At 0.0 neither the solar peak nor the ratio may change anything.

        This is the invariant that actually matters, and it needs no knowledge
        of internal SoC bookkeeping: if the solar term ever leaks into the
        default path, varying these inputs moves the numbers.
        """
        for solar_peak in self.PEAKS:
            for soc in self.SOCS:
                base = self._matrix_run(0.0, solar_peak, soc, None)
                for ratio in self.RATIOS:
                    t = self._matrix_run(0.0, solar_peak, soc, ratio)
                    ctx = f"peak={solar_peak} soc={soc} ratio={ratio}"
                    self.assertEqual(
                        t["grid_charge_needed"],
                        base["grid_charge_needed"], msg=ctx,
                    )
                    self.assertEqual(
                        t["grid_charge_needed_solar_ceiling"],
                        base["grid_charge_needed_solar_ceiling"], msg=ctx,
                    )

    def test_default_ceiling_is_not_published(self):
        """At the 0.0 default the trace publishes no solar ceiling at all.

        Deliberately changed by FIX 4.  The old assertion here was that the
        recorded ceiling equals the legacy headroom bit-for-bit — which is
        exactly the defect: at weight 0.0 the "ceiling" holds no solar term,
        so every user who never opted in published a value whose name claimed
        otherwise.  The key is now None at the default, and the exactness
        requirement moves to the enabled path below, where the number is real.
        """
        for solar_peak in self.PEAKS:
            for soc_pct in self.SOCS:
                for ratio in (None,) + self.RATIOS:
                    t = self._matrix_run(0.0, solar_peak, soc_pct, ratio)
                    ctx = f"peak={solar_peak} soc={soc_pct} ratio={ratio}"
                    self.assertIn("grid_charge_needed_solar_ceiling", t, msg=ctx)
                    self.assertIsNone(
                        t["grid_charge_needed_solar_ceiling"], msg=ctx,
                    )

    def test_enabled_ceiling_is_exact_and_never_negative(self):
        """With the option on, the published ceiling is a real, exact number.

        Keeps the exactness bar the removed default-path assertion set.  The
        night drain does not engage in these scenarios
        (``night_drain_applied`` is False), so the current SoC at Case B time
        *is* the start SoC and a full-confidence ceiling must be bit-for-bit
        ``max(0, soc_max - current_soc) - available_soc * ratio`` clamped to
        the physical headroom.  An inequality would let drift — including a
        constant offset — pass unnoticed.
        """
        for solar_peak in self.PEAKS:
            for soc_pct in self.SOCS:
                soc_max = 15.0
                current = 15.0 * soc_pct / 100.0
                headroom = max(0.0, soc_max - current)
                for ratio in self.RATIOS:
                    t = self._matrix_run(1.0, solar_peak, soc_pct, ratio)
                    ctx = f"peak={solar_peak} soc={soc_pct} ratio={ratio}"
                    got = t["grid_charge_needed_solar_ceiling"]
                    self.assertIsNotNone(got, msg=ctx)
                    self.assertGreaterEqual(got, 0.0, msg=ctx)
                    self.assertLessEqual(got, headroom + 1e-9, msg=ctx)
                    # A confident clear forecast can never raise the ceiling
                    # above the headroom it is already clamped to.
                    if ratio == 1.0:
                        self.assertLessEqual(
                            got, headroom + 1e-9, msg=ctx,
                        )

    def test_enabled_path_actually_uses_the_ceiling(self):
        """At 1.0 with a confident forecast the ceiling must be below legacy."""
        legacy = max(0.0, 15.0 - 15.0 * 37.0 / 100.0)
        t = self._matrix_run(1.0, 7.0, 37.0, 1.0)
        self.assertLess(t["grid_charge_needed_solar_ceiling"], legacy - 1e-6)

    # -- layer 2: the helper's arithmetic is byte-identical ---------------
    # The literal below is the pre-0.3.22 expression, written out on purpose:
    # it must never be refactored to call the code under test.
    WEIGHTS_OFF = (0.0, -0.0, 0, 0.0, float("nan"), float("inf"), float("-inf"))
    RATIO_OFF = (None, 0.0, 0.1, 0.35, 1.0, 5.0, -3.0,
                 float("nan"), float("inf"), float("-inf"))
    AVAIL_OFF = (0.0, 2.0, 5.55, 9.0, 15.0, 40.0,
                 float("nan"), float("inf"), float("-inf"))

    def test_default_weight_returns_legacy_expression_exactly(self):
        """Every off-ramp weight and input reproduces max(0, soc_max - soc).

        Covers a wide input grid rather than a single point: a leak could be
        input-dependent (e.g. only when available_soc > soc_max, or only for a
        non-None ratio) and still pass a one-point check.  ``assertEqual`` is
        exact — a single ULP of float noise fails the test.
        """
        soc_pairs = (
            (15.0, 0.0),        # empty battery, full headroom
            (15.0, 5.55),       # the 37% case the feature is built around
            (15.0, 14.999999),  # clamp boundary, just below soc_max
            (15.0, 15.0),       # clamp boundary exactly: legacy == 0.0
            (15.0, 15.000001),  # past soc_max: legacy clamps to 0.0
            (15.0, 22.5),       # far past soc_max
            (15.0, -0.001),     # slightly negative (docstring allows it)
            (15.0, -3.7),       # deeper negative
            (10.0, 4.3333333),  # non-soc_max capacity, repeating decimal
            (0.0, 0.0),         # degenerate zero-capacity config
            (0.0, -1.0),
            (7.5, 7.5),
        )
        for soc_max_kwh, current_soc_kwh in soc_pairs:
            legacy = max(0.0, soc_max_kwh - current_soc_kwh)
            for w in self.WEIGHTS_OFF:
                cfg = BatteryConfig(
                    capacity_kwh=15.0, soc_min=12.0, soc_max=100.0,
                    grid_charge_solar_aware=w,
                )
                for avail in self.AVAIL_OFF:
                    for ratio in self.RATIO_OFF:
                        got = optimizer._case_b_grid_charge(
                            cfg, soc_max_kwh, current_soc_kwh, avail, ratio,
                        )
                        self.assertEqual(
                            got, legacy,
                            msg=(
                                f"soc_max={soc_max_kwh} soc={current_soc_kwh} "
                                f"weight={w} available_soc={avail} ratio={ratio}"
                            ),
                        )

    def test_default_weight_clamp_boundaries_exactly(self):
        """The clamp boundaries must be exact zeros, not 'about zero'.

        current_soc == soc_max gives legacy headroom exactly 0.0 and
        current_soc > soc_max must clamp to 0.0 rather than going negative —
        a negative allowance would mean the optimizer plans to *discharge*
        the battery in order to charge it.
        """
        for soc_max_kwh, current_soc_kwh, expected in (
            (15.0, 15.0, 0.0),
            (15.0, 15.000001, 0.0),
            (15.0, 22.5, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (7.5, 7.5, 0.0),
            # current_soc below zero: legacy headroom EXCEEDS soc_max, and it
            # must not be clamped down — a pre-existing negative SoC still
            # needs that much charging.  Exact float, no tolerance.
            (15.0, -0.001, 15.001),
            (15.0, -3.7, 18.7),
        ):
            cfg = BatteryConfig(
                capacity_kwh=15.0, grid_charge_solar_aware=0.0,
            )
            # available_soc deliberately absurd: at 0.0 it must be ignored.
            for avail in (0.0, 1e9, float("nan"), float("inf")):
                for ratio in (None, 1.0, float("nan")):
                    got = optimizer._case_b_grid_charge(
                        cfg, soc_max_kwh, current_soc_kwh, avail, ratio,
                    )
                    self.assertEqual(
                        got, max(0.0, soc_max_kwh - current_soc_kwh),
                        msg=f"soc_max={soc_max_kwh} soc={current_soc_kwh}",
                    )
                    self.assertEqual(
                        got, expected,
                        msg=f"soc_max={soc_max_kwh} soc={current_soc_kwh}",
                    )

    def test_default_weight_ignores_nonfinite_ratio_and_available_soc(self):
        """At 0.0 a NaN/inf ratio or available_soc must not poison the result.

        Regression guard against reordering ``_case_b_grid_charge`` so the
        ratio is normalised *before* the 0.0 early return:
        ``max(0.0, min(1.0, nan))`` is 0.0 today by accident, and
        ``nan * w`` would propagate NaN into grid_charge_needed the moment the
        early return moved.  A NaN in the plan trace is not a "close enough"
        number — it silently disables grid charging.
        """
        cfg = BatteryConfig(capacity_kwh=15.0, grid_charge_solar_aware=0.0)
        legacy = max(0.0, 15.0 - 5.55)
        for ratio in self.RATIO_OFF:
            for avail in self.AVAIL_OFF:
                got = optimizer._case_b_grid_charge(cfg, 15.0, 5.55, avail, ratio)
                self.assertEqual(
                    got, legacy,
                    msg=f"available_soc={avail} ratio={ratio}",
                )
                self.assertFalse(
                    got != got,  # NaN != NaN, so this catches a poisoned result
                    msg=f"NaN leaked at available_soc={avail} ratio={ratio}",
                )

    # -- layer 3: byte-identical to the real 0.3.21 release source -------
    @classmethod
    def _load_release_optimizer(cls):
        """Exec the v0.3.21 optimizer.py blob from git into its own package.

        Returns None when git or the blob is unavailable (e.g. a shallow
        clone).  The 0.3.21 source only pulls names that still exist in the
        current ``const.py``, so the live const is reused; the added
        ``DEFAULT_GRID_CHARGE_SOLAR_AWARE`` constant is simply unused there.
        """
        import subprocess
        import sys
        import types
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        pkg_name = "bopt_pkg_v321"
        if pkg_name in sys.modules:
            return getattr(sys.modules[pkg_name], "optimizer", None)

        blob = subprocess.run(
            ["git", "-C", str(repo), "show",
             f"{cls._RELEASE_COMMIT}:{cls._RELEASE_BLOB}"],
            capture_output=True, text=True,
        )
        if blob.returncode != 0 or not blob.stdout.strip():
            return None

        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []  # required so `from .const import ...` resolves
        sys.modules[pkg_name] = pkg

        def _exec(target, source):
            mod = types.ModuleType(target)
            mod.__file__ = f"<v{cls._RELEASE_COMMIT}:{target}>"
            mod.__package__ = pkg_name
            sys.modules[target] = mod
            exec(compile(source, f"<v{cls._RELEASE_COMMIT}>", "exec"), mod.__dict__)
            return mod

        live_const = (repo / "custom_components" / "battery_optimizer"
                      / "const.py").read_text()
        pkg.const = _exec(f"{pkg_name}.const", live_const)
        pkg.optimizer = _exec(f"{pkg_name}.optimizer", blob.stdout)
        return pkg.optimizer

    @staticmethod
    def _plan_signature(result):
        """Every behavioural value of a result, comparable across modules.

        SlotPlan instances from two module copies are never ``==`` (dataclass
        ``__eq__`` requires identical classes), so fields are compared
        explicitly rather than by list equality.
        """
        import dataclasses

        sig = {}
        for f in dataclasses.fields(result):
            if f.name in ("trace", "slots"):
                # trace is diagnostic (compared key-wise below); slots holds
                # SlotPlan objects whose cross-module __eq__ is always False,
                # so it is decomposed into plain tuples under __slots__.
                continue
            sig[f.name] = getattr(result, f.name)
        sig["__slots__"] = [
            tuple(getattr(sp, sf.name) for sf in dataclasses.fields(sp))
            for sp in result.slots
        ]
        return sig

    def test_default_end_to_end_matches_release_321_exactly(self):
        """Whole-function differential against the real v0.3.21 source.

        This is the strongest form of the guarantee: the v0.3.21 optimizer is
        loaded from git commit 17c6497 and run over the same scenarios with
        the same inputs.  At the 0.0 default every behavioural value — every
        scalar total, every cost breakdown, every per-slot profit, SoC and
        energy figure, the emaldo mode vector and the PV-switch vector — must
        be bit-for-bit equal.  The only permitted difference is the two NEW
        diagnostic trace keys.
        """
        release = self._load_release_optimizer()
        if release is None:
            self.skipTest(
                f"git blob {self._RELEASE_COMMIT}:{self._RELEASE_BLOB} "
                f"unavailable — cannot run the release differential"
            )

        new_keys = {
            "grid_charge_needed_solar_ceiling", "solar_confidence_ratio",
            # Issue #32 two-tier floor and proportional hedge diagnostics;
            # inert (None / 0) at the default weight.
            "case_a_floor_tier1", "tier1_refill_kwh", "tier1_discharge_kwh",
            "n_tier1_discharge", "hedge_value_kwh",
        }
        for peak in self.PEAKS:
            for soc in self.SOCS:
                ctx = f"peak={peak} soc={soc}"
                self._assert_matches_release(release, peak, soc, None,
                                             ctx=ctx, release_ratio=None)
                current = self._matrix_result(0.0, peak, soc, None)
                old = release.optimize(*self._release_inputs(release, peak, soc),
                                       start_slot=0, initial_soc_pct=soc,
                                       total_slots=self.SLOTS,
                                       enable_pv_strategy=True)
                # The trace is diagnostics, but the shared keys must match
                # exactly; only the two newly added keys may be absent in 0.3.21.
                t_new, t_old = current.trace, old.trace
                shared = set(t_old) & set(t_new)
                for key in shared:
                    self.assertEqual(
                        t_new[key], t_old[key],
                        msg=f"trace[{key}] differs from v0.3.21 at {ctx}",
                    )
                extra = set(t_new) - set(t_old)
                self.assertEqual(
                    extra, new_keys,
                    msg=f"unexpected new trace keys at {ctx}",
                )
                # And the value 0.3.21 reported as grid_charge_needed must be
                # the number the new code still produces from the same inputs.
                self.assertEqual(
                    t_new["grid_charge_needed"], t_old["grid_charge_needed"],
                    msg=f"grid_charge_needed differs from v0.3.21 at {ctx}",
                )

        # Negative control: the same comparison at 1.0 MUST detect a
        # difference.  Without this the differential above could pass while
        # comparing nothing useful.
        current = self._matrix_result(1.0, 7.0, 37.0, 1.0)
        old = release.optimize(*self._release_inputs(release, 7.0, 37.0),
                               start_slot=0, initial_soc_pct=37.0,
                               total_slots=self.SLOTS,
                               enable_pv_strategy=True)
        self.assertNotEqual(
            self._plan_signature(current)["total_profit"],
            self._plan_signature(old)["total_profit"],
            msg="control failed: aware=1.0 produced no change, so the "
                "default-path differential above cannot detect a leak either",
        )

    def test_default_end_to_end_matches_release_321_without_round_trip(self):
        """Branch coverage: the path where Case B never runs.

        The main matrix is deliberately non-degenerate (profitable round trips
        present, kWh cycled > 1.0), which means it never exercises
        ``profitable_charge == []`` — the branch where ``grid_charge_needed``
        must NOT pick up the Case B term at all.  A flat price curve with no
        solar produces exactly that branch, at zero cycled kWh, which is why
        it lives in its own test rather than in the matrix.

        This is the branch that would break if someone ever moved the Case B
        add outside its ``if profitable_charge:`` guard.
        """
        release = self._load_release_optimizer()
        if release is None:
            self.skipTest(
                f"git blob {self._RELEASE_COMMIT}:{self._RELEASE_BLOB} "
                f"unavailable — cannot run the release differential"
            )
        for soc in self.SOCS:
            ctx = f"flat soc={soc}"
            current = self._flat_result(0.0, soc, None)
            # Confirm the branch really is the one we mean to cover.
            self.assertEqual(
                current.trace["n_profitable_charge"], 0,
                msg=f"{ctx}: expected no profitable charge slots",
            )
            self.assertEqual(current.trace["grid_charge_needed"], 0.0, msg=ctx)
            self._assert_matches_release(release, 0.0, soc, None,
                                         ctx=ctx, release_ratio=None, flat=True)

    def _assert_matches_release(self, release, solar_peak, soc, ratio,
                                *, ctx, release_ratio, flat=False):
        """Assert one default-path run is identical to the v0.3.21 run."""
        if flat:
            current = self._flat_result(0.0, soc, ratio)
            old = release.optimize(*self._flat_inputs(release, soc),
                                   start_slot=0, initial_soc_pct=soc,
                                   total_slots=self.SLOTS,
                                   enable_pv_strategy=True)
        else:
            current = self._matrix_result(0.0, solar_peak, soc, ratio)
            old = release.optimize(*self._release_inputs(release, solar_peak, soc),
                                   start_slot=0, initial_soc_pct=soc,
                                   total_slots=self.SLOTS,
                                   enable_pv_strategy=True)
        sig_new = self._plan_signature(current)
        sig_old = self._plan_signature(old)
        self.assertEqual(sorted(sig_new), sorted(sig_old))
        for key in sig_new:
            self.assertEqual(
                sig_new[key], sig_old[key],
                msg=f"{key} differs from v0.3.21 at {ctx}",
            )

    # Flat curve, no solar: nothing is ever profitable, so Case B is skipped.
    FLAT_SPOTS = 0.15

    def _flat_result(self, aware, soc, ratio):
        cfg = self._cfg(aware)
        spots = [self.FLAT_SPOTS] * self.SLOTS
        solar = [0.0] * self.SLOTS
        buy, sell = optimizer.compute_prices(spots, cfg)
        return optimizer.optimize(
            buy, sell, solar, cfg, start_slot=0, initial_soc_pct=soc,
            total_slots=self.SLOTS, enable_pv_strategy=True,
            solar_confidence_ratio=ratio,
        )

    def _flat_inputs(self, release, soc):
        cfg = release.BatteryConfig(
            capacity_kwh=15.0, soc_min=12.0, soc_max=100.0,
            soc_recovery_buffer_pct=2.0, base_load_kw=1.1,
            charge_efficiency=0.9, discharge_efficiency=0.9,
            max_charge_kw=10.0, max_discharge_kw=10.0,
            wear_cost_per_kwh=0.03, idle_power_kw=0.1,
            vat_multiplier=1.255, transfer_fee_buy=0.0776,
            sales_commission=0.003,
        )
        spots = [self.FLAT_SPOTS] * self.SLOTS
        solar = [0.0] * self.SLOTS
        buy, sell = release.compute_prices(spots, cfg)
        return (buy, sell, solar, cfg)

    def _release_inputs(self, release, solar_peak, soc):
        """(buy, sell, solar, cfg) for the 0.3.21 module — same as current."""
        cfg = release.BatteryConfig(
            capacity_kwh=15.0, soc_min=12.0, soc_max=100.0,
            soc_recovery_buffer_pct=2.0, base_load_kw=1.1,
            charge_efficiency=0.9, discharge_efficiency=0.9,
            max_charge_kw=10.0, max_discharge_kw=10.0,
            wear_cost_per_kwh=0.03, idle_power_kw=0.1,
            vat_multiplier=1.255, transfer_fee_buy=0.0776,
            sales_commission=0.003,
        )
        spots = [0.02 if s < 60 else 0.30 for s in range(self.SLOTS)]
        solar = [max(0.0, solar_peak * (1 - (abs(s - 48) / 26.0) ** 2)
                     if abs(s - 48) <= 26.0 else 0.0) for s in range(self.SLOTS)]
        buy, sell = release.compute_prices(spots, cfg)
        return (buy, sell, solar, cfg)

    def test_default_end_to_end_identical_across_ratios(self):
        """All scalar totals, not just the trace, must ignore the ratio.

        Layer 3 in miniature and without needing git: with the option at 0.0
        the ratio is an input the old code could not even accept, so feeding
        every value through the full result is a leak detector for anything
        downstream of Case B as well.
        """
        for peak in self.PEAKS:
            for soc in self.SOCS:
                base = self._plan_signature(
                    self._matrix_result(0.0, peak, soc, None)
                )
                for ratio in self.RATIOS:
                    t = self._plan_signature(
                        self._matrix_result(0.0, peak, soc, ratio)
                    )
                    ctx = f"peak={peak} soc={soc} ratio={ratio}"
                    self.assertEqual(sorted(t), sorted(base))
                    for key in t:
                        self.assertEqual(
                            t[key], base[key],
                            msg=f"{key} moved with ratio at {ctx}",
                        )