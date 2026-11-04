"""User schedule rules tests — pure module, no HA imports."""
from __future__ import annotations

import unittest
from datetime import date

from . import load_rules, load_optimizer

rules_mod, const = load_rules()
UserRule = rules_mod.UserRule
SlotWinner = rules_mod.SlotWinner
LEVEL_DEFAULT = rules_mod.LEVEL_DEFAULT
LEVEL_WEEKDAY = rules_mod.LEVEL_WEEKDAY
LEVEL_DATE = rules_mod.LEVEL_DATE


def weekday_rule(days, start, end, action="charge", soc=90, pv="inherit", label=""):
    return UserRule(
        level=LEVEL_WEEKDAY, days=days, start_date=None, end_date=None,
        start_time=start, end_time=end, action=action, soc_target=soc,
        pv_sell=pv, label=label,
    )


def default_rule(action="optimizer", soc=None, pv="inherit"):
    return UserRule(
        level=LEVEL_DEFAULT, days=[], start_date=None, end_date=None,
        start_time="00:00", end_time="24:00", action=action,
        soc_target=soc, pv_sell=pv, label="Default",
    )


def date_rule(start_date, end_date, start, end, action="charge", soc=90, pv="inherit"):
    return UserRule(
        level=LEVEL_DATE, days=[], start_date=start_date, end_date=end_date,
        start_time=start, end_time=end, action=action, soc_target=soc,
        pv_sell=pv, label="",
    )


class ValidationTests(unittest.TestCase):
    def test_valid_weekday_rule(self):
        self.assertEqual(rules_mod.rule_errors(
            weekday_rule([0, 1], "07:00", "17:00"), []), [])

    def test_charge_requires_soc_target(self):
        errs = rules_mod.rule_errors(
            UserRule(level=LEVEL_WEEKDAY, days=[0], start_date=None,
                     end_date=None, start_time="07:00", end_time="17:00",
                     action="charge", soc_target=None, pv_sell="inherit",
                     label=""), [])
        self.assertTrue(any("soc" in e.lower() for e in errs))

    def test_weekday_no_midnight_crossing(self):
        errs = rules_mod.rule_errors(
            weekday_rule([0], "22:00", "06:00"), [])
        self.assertTrue(any("midnight" in e.lower() or "end" in e.lower() for e in errs))

    def test_date_overnight_allowed(self):
        errs = rules_mod.rule_errors(
            date_rule("2026-08-18", "2026-08-19", "19:45", "01:15"), [])
        self.assertEqual(errs, [])

    def test_15min_alignment_rejected(self):
        errs = rules_mod.rule_errors(
            weekday_rule([0], "07:13", "17:00"), [])
        self.assertTrue(any("15" in e for e in errs))

    def test_same_level_overlap_rejected(self):
        siblings = [weekday_rule([0], "07:00", "17:00", label="A")]
        errs = rules_mod.rule_errors(
            weekday_rule([0], "12:00", "20:00", label="B"), siblings)
        self.assertTrue(any("overlap" in e.lower() for e in errs))

    def test_adjacent_windows_allowed(self):
        siblings = [weekday_rule([0], "07:00", "12:00", label="A")]
        errs = rules_mod.rule_errors(
            weekday_rule([0], "12:00", "17:00", label="B"), siblings)
        self.assertEqual(errs, [])

    def test_weekday_overlap_requires_shared_day(self):
        siblings = [weekday_rule([0], "07:00", "17:00", label="A")]
        errs = rules_mod.rule_errors(
            weekday_rule([1], "12:00", "20:00", label="B"), siblings)
        self.assertEqual(errs, [])

    def test_cross_level_overlap_allowed(self):
        siblings = [weekday_rule([0], "07:00", "17:00", label="A")]
        errs = rules_mod.rule_errors(
            date_rule("2026-08-18", None, "09:00", "11:00"), siblings)
        self.assertEqual(errs, [])

    def test_soc_target_ignored_for_original(self):
        r = UserRule(level=LEVEL_WEEKDAY, days=[0], start_date=None,
                     end_date=None, start_time="07:00", end_time="17:00",
                     action="original", soc_target=42, pv_sell="inherit",
                     label="")
        self.assertEqual(rules_mod.rule_errors(r, []), [])

    def test_single_day_overnight_rejected(self):
        errs = rules_mod.rule_errors(
            date_rule("2026-08-18", "2026-08-18", "22:00", "04:00"), [])
        self.assertTrue(any("overnight" in e.lower() or "end date" in e.lower()
                            for e in errs))

    def test_single_day_overnight_no_end_date_rejected(self):
        errs = rules_mod.rule_errors(
            date_rule("2026-08-18", None, "22:00", "04:00"), [])
        self.assertTrue(any("overnight" in e.lower() for e in errs))

    def test_multi_day_overnight_still_allowed(self):
        self.assertEqual(
            rules_mod.rule_errors(
                date_rule("2026-08-18", "2026-08-19", "22:00", "04:00"), []),
            [])


class ExpansionTests(unittest.TestCase):
    def test_default_rule_covers_all_slots(self):
        winners = rules_mod.expand_day([default_rule()], date(2026, 8, 17))
        self.assertEqual(len(winners), 96)
        self.assertTrue(all(w.action == "optimizer" and w.rule_index is None
                            for w in winners))

    def test_weekday_rule_applies_only_that_weekday(self):
        # 2026-08-17 is a Monday (weekday 0)
        rules = [default_rule(), weekday_rule([0], "07:00", "17:00", action="idle")]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        self.assertEqual(winners[0].action, "optimizer")  # 00:00 not in window
        self.assertEqual(winners[28].action, "idle")      # 07:00 = slot 28
        self.assertEqual(winners[67].action, "idle")      # 16:45 in window
        self.assertEqual(winners[68].action, "optimizer") # 17:00 exclusive
        # Tuesday: rule inactive
        winners_tue = rules_mod.expand_day(rules, date(2026, 8, 18))
        self.assertTrue(all(w.action == "optimizer" for w in winners_tue))

    def test_date_rule_beats_weekday(self):
        rules = [
            default_rule(),
            weekday_rule([0], "07:00", "17:00", action="idle"),
            date_rule("2026-08-17", None, "09:00", "11:00", action="charge", soc=100),
        ]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        self.assertEqual(winners[36].action, "charge")   # 09:00 = slot 36
        self.assertEqual(winners[36].soc_target, 100)
        self.assertEqual(winners[37].action, "charge")
        self.assertEqual(winners[43].action, "charge")   # 10:45 still in date window
        self.assertEqual(winners[44].action, "idle")     # 11:00 date rule ended
        self.assertEqual(winners[0].action, "optimizer") # 00:00 default only

    def test_overnight_date_rule_two_segments(self):
        rules = [default_rule(),
                 date_rule("2026-08-18", "2026-08-19", "19:45", "01:15",
                           action="idle")]
        # Day 1 (18th): covers 19:45-24:00
        winners1 = rules_mod.expand_day(rules, date(2026, 8, 18))
        self.assertEqual(winners1[79].action, "idle")   # 19:45 = slot 79
        self.assertEqual(winners1[95].action, "idle")   # 23:45
        self.assertEqual(winners1[0].action, "optimizer")  # 00:00 not covered day 1
        # Day 2 (19th): covers 00:00-01:15
        winners2 = rules_mod.expand_day(rules, date(2026, 8, 19))
        self.assertEqual(winners2[0].action, "idle")    # 00:00 = slot 0
        self.assertEqual(winners2[4].action, "idle")    # 01:00 in window
        self.assertEqual(winners2[5].action, "optimizer")  # 01:15 exclusive

    def test_multi_day_middle_days_covered(self):
        rules = [default_rule(),
                 date_rule("2026-08-18", "2026-08-20", "10:00", "14:00",
                           action="idle")]
        winners = rules_mod.expand_day(rules, date(2026, 8, 19))
        self.assertEqual(winners[40].action, "idle")    # 10:00 middle day
        self.assertEqual(winners[55].action, "idle")    # 13:45
        self.assertEqual(winners[56].action, "optimizer")  # 14:00


class MaskTests(unittest.TestCase):
    def test_optimizer_bytes_preserved_default(self):
        opt = [128] * 96
        pv = [True] * 96
        winners = rules_mod.expand_day([default_rule()], date(2026, 8, 17))
        masked, mpv, sources, pvs = rules_mod.mask_plan(opt, pv, winners)
        self.assertEqual(masked, opt)
        self.assertEqual(mpv, pv)
        self.assertEqual(set(sources), {"optimizer"})
        self.assertEqual(set(pvs), {"optimizer"})

    def test_user_charge_idle_discharge_bytes(self):
        opt = [128] * 96
        pv = [True] * 96
        rules = [
            default_rule(),
            weekday_rule([0], "07:00", "08:00", action="charge", soc=100),
            weekday_rule([0], "08:00", "09:00", action="idle"),
            weekday_rule([0], "09:00", "10:00", action="discharge", soc=40),
            weekday_rule([0], "10:00", "11:00", action="original"),
        ]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        masked, _, sources, _ = rules_mod.mask_plan(opt, pv, winners)
        self.assertEqual(masked[28], 100)     # 07:00 charge@100
        self.assertEqual(masked[32], 0)       # 08:00 idle
        self.assertEqual(masked[36], 256 - 40)  # 09:00 discharge to 40% = 216
        self.assertEqual(masked[40], 128)     # 10:00 original
        self.assertEqual(sources[28], "user")
        self.assertEqual(sources[36], "user")
        self.assertEqual(sources[40], "internal")
        self.assertEqual(sources[0], "optimizer")

    def test_pv_mask_inherit_sell_charge(self):
        opt = [128] * 96
        pv = [True] * 96
        rules = [
            default_rule(),
            weekday_rule([0], "07:00", "08:00", action="idle", pv="sell"),
            weekday_rule([0], "08:00", "09:00", action="idle", pv="charge"),
            weekday_rule([0], "09:00", "10:00", action="idle", pv="inherit"),
        ]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        masked, mpv, sources, pvs = rules_mod.mask_plan(opt, pv, winners)
        self.assertFalse(mpv[28])             # 07:00 sell → PV off
        self.assertTrue(mpv[32])              # 08:00 charge → PV on
        self.assertTrue(mpv[36])              # 09:00 inherit → optimizer True
        self.assertEqual(pvs[28], "user")
        self.assertEqual(pvs[32], "user")
        self.assertEqual(pvs[36], "optimizer")

    def test_default_rule_editable_to_original(self):
        opt = [0] * 96  # optimizer plan says idle everywhere
        pv = [True] * 96
        rules = [default_rule(action="original")]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        masked, _, sources, _ = rules_mod.mask_plan(opt, pv, winners)
        self.assertEqual(masked, [128] * 96)
        self.assertEqual(set(sources), {"internal"})


class SourcesSummaryTests(unittest.TestCase):
    def test_summary_counts_user_slots_only(self):
        sources = ["user"] * 4 + ["optimizer"] * 92
        bytes_ = [100, 0, 216, 128] + [128] * 92
        self.assertEqual(
            rules_mod.sources_summary(sources, bytes_), "1C 1D 2I"
        )


class RuleSummaryTests(unittest.TestCase):
    def test_weekday_charge_summary(self):
        r = weekday_rule([0, 1, 2, 3, 4], "07:00", "17:00", action="charge", soc=90)
        self.assertEqual(rules_mod.rule_summary(r),
                         "Charge to 90% · Mon–Fri · 07:00–17:00")

    def test_non_consecutive_weekdays(self):
        r = weekday_rule([5, 6], "17:00", "23:00", action="discharge", soc=40)
        self.assertEqual(rules_mod.rule_summary(r),
                         "Discharge to 40% · Sat, Sun · 17:00–23:00")

    def test_date_range(self):
        r = date_rule("2026-08-18", "2026-08-20", "10:00", "14:00", action="idle")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Idle · 2026-08-18–2026-08-20 · 10:00–14:00")

    def test_default_optimizer(self):
        self.assertEqual(rules_mod.rule_summary(default_rule()),
                         "Optimizer")

    def test_default_full_day_optimizer(self):
        self.assertEqual(rules_mod.rule_summary(default_rule()), "Optimizer")

    def test_default_narrowed_window(self):
        r = default_rule(action="discharge", soc=15)
        r.start_time, r.end_time = "22:00", "06:00"
        self.assertEqual(rules_mod.rule_summary(r),
                         "Discharge to 15% · 22:00–06:00")

    def test_default_rule_title_optimizer(self):
        self.assertEqual(rules_mod.default_rule_title(default_rule()),
                         "Default Schedule (Optimizer)")

    def test_default_rule_title_original(self):
        r = default_rule(action="original")
        self.assertEqual(rules_mod.default_rule_title(r),
                         "Default Schedule (Original)")

    def test_default_rule_title_charge(self):
        r = default_rule(action="charge", soc=90)
        self.assertEqual(rules_mod.default_rule_title(r),
                         "Default Schedule (Charge to 90%)")

    def test_default_rule_title_narrowed_window(self):
        r = default_rule(action="discharge", soc=15)
        r.start_time, r.end_time = "22:00", "06:00"
        self.assertEqual(rules_mod.default_rule_title(r),
                         "Default Schedule (Discharge to 15% · 22:00–06:00)")

    def test_weekday_single_day(self):
        r = weekday_rule([1], "08:00", "09:00", action="idle")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Idle · Tue · 08:00–09:00")

    def test_weekday_split_runs(self):
        r = weekday_rule([0, 1, 2, 4, 5], "07:00", "17:00", action="charge", soc=80)
        self.assertEqual(rules_mod.rule_summary(r),
                         "Charge to 80% · Mon–Wed, Fri–Sat · 07:00–17:00")

    def test_weekday_full_week(self):
        r = weekday_rule([0, 1, 2, 3, 4, 5, 6], "00:00", "24:00", action="idle")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Idle · Every day · 00:00–24:00")

    def test_weekday_empty_days(self):
        r = weekday_rule([], "07:00", "17:00", action="idle")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Idle · Weekday · 07:00–17:00")

    def test_date_single_day(self):
        r = date_rule("2026-08-18", None, "10:00", "14:00", action="idle")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Idle · 2026-08-18 · 10:00–14:00")

    def test_charge_without_target_placeholder(self):
        r = UserRule(level=LEVEL_WEEKDAY, days=[0], start_date=None,
                     end_date=None, start_time="07:00", end_time="17:00",
                     action="charge", soc_target=None, pv_sell="inherit",
                     label="")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Charge to ?% · Mon · 07:00–17:00")

    def test_original_action(self):
        r = weekday_rule([0], "07:00", "17:00", action="original")
        self.assertEqual(rules_mod.rule_summary(r),
                         "Original · Mon · 07:00–17:00")

    def test_default_narrowed_no_target_placeholder(self):
        r = default_rule(action="charge", soc=None)
        r.start_time, r.end_time = "07:00", "17:00"
        self.assertEqual(rules_mod.rule_summary(r),
                         "Charge to ?% · 07:00–17:00")


class MaskResimSmokeTests(unittest.TestCase):
    def test_masked_plan_resims_trajectory(self):
        optimizer, const = load_optimizer()
        rules_mod = load_rules()[0]
        cfg = optimizer.BatteryConfig(capacity_kwh=15.0, soc_min=20.0, soc_max=100.0,
                                      max_charge_kw=5.0, max_discharge_kw=5.0)
        solar = [0.0] * 96
        buy = [0.1] * 96
        sell = [0.0] * 96
        result = optimizer.optimize(buy, sell, solar, cfg, start_slot=0,
                                    initial_soc_pct=50.0)
        # user rule: force idle everywhere (byte 0) except nothing
        winners = rules_mod.expand_day(
            [rules_mod.UserRule(level="default", start_time="00:00",
                                end_time="24:00", action="optimizer")],
            date(2026, 8, 17),
        )
        opt = result.slot_values
        pv = result.thirdparty_pv_slots
        masked, mpv, sources, pvs = rules_mod.mask_plan(opt, pv, winners)
        self.assertEqual(masked, opt)  # optimizer action -> unchanged

        # manual idle rule overrides everything
        rules = [
            rules_mod.UserRule(level="default", start_time="00:00",
                               end_time="24:00", action="idle"),
        ]
        winners = rules_mod.expand_day(rules, date(2026, 8, 17))
        masked, mpv, sources, pvs = rules_mod.mask_plan(opt, pv, winners)
        self.assertTrue(all(b == 0 for b in masked))
        self.assertEqual(set(sources), {"user"})
