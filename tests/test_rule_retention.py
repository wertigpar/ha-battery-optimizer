"""Unit tests for expired date-rule retention (pure rules.py helper)."""
from datetime import date, timedelta
from types import SimpleNamespace

from custom_components.battery_optimizer.rules import expired_rule_ids

TODAY = date(2026, 8, 26)


def _sub(
    sid: str,
    *,
    subentry_type: str = "rule",
    level: str = "date",
    end_date: str | None = "2026-08-20",
) -> SimpleNamespace:
    return SimpleNamespace(
        subentry_id=sid,
        subentry_type=subentry_type,
        data={"level": level, "end_date": end_date},
    )


def test_retention_zero_never_deletes():
    old = _sub("s_old")  # end_date is 6 days before TODAY
    assert expired_rule_ids([old], TODAY, 0) == []
    assert expired_rule_ids([old], TODAY, -1) == []


def test_retention_one_deletes_yesterday_not_today():
    today_rule = _sub("s_today", end_date=TODAY.isoformat())
    yday_rule = _sub("s_yday", end_date=(TODAY - timedelta(days=1)).isoformat())
    assert expired_rule_ids([today_rule], TODAY, 1) == []
    assert expired_rule_ids([yday_rule], TODAY, 1) == ["s_yday"]


def test_retention_boundaries():
    # retention 7: age 6 survives, age 7 released
    age6 = _sub("a6", end_date=(TODAY - timedelta(days=6)).isoformat())
    age7 = _sub("a7", end_date=(TODAY - timedelta(days=7)).isoformat())
    assert expired_rule_ids([age6, age7], TODAY, 7) == ["a7"]
    # retention 30: age 29 survives, age 30 released
    age29 = _sub("b29", end_date=(TODAY - timedelta(days=29)).isoformat())
    age30 = _sub("b30", end_date=(TODAY - timedelta(days=30)).isoformat())
    assert expired_rule_ids([age29, age30], TODAY, 30) == ["b30"]


def test_weekday_and_default_never_deleted():
    wd = _sub("s_wd", level="weekday", end_date=None)
    df = _sub("s_def", level="default", end_date=None)
    assert expired_rule_ids([wd, df], TODAY, 1) == []


def test_date_rule_without_end_date_never_deleted():
    no_end = _sub("s_noend", end_date=None)
    assert expired_rule_ids([no_end], TODAY, 7) == []


def test_non_rule_subentries_ignored():
    device = _sub("s_dev", subentry_type="device", end_date="2026-08-01")
    assert expired_rule_ids([device], TODAY, 1) == []


def test_retroactive_old_rule_deleted_on_first_sweep():
    ancient = _sub("s_ancient", end_date="2026-01-01")
    assert expired_rule_ids([ancient], TODAY, 7) == ["s_ancient"]


def test_bad_end_date_skipped():
    bad = _sub("s_bad", end_date="not-a-date")
    assert expired_rule_ids([bad], TODAY, 1) == []