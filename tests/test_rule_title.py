"""Tests for the rule-title builder (disabled-suffix on rule titles)."""

from __future__ import annotations

from custom_components.battery_optimizer.rules import (
    default_rule_title,
    rule_from_data,
    rule_title,
)


def _rule(**overrides) -> object:
    base = {
        "level": "date",
        "action": "discharge",
        "start_time": "08:30",
        "end_time": "09:30",
        "soc_target": 20,
        "pv_sell": "inherit",
        "label": "",
        "enabled": True,
        "start_date": "2026-08-26",
        "end_date": "2026-08-26",
    }
    base.update(overrides)
    return rule_from_data(base)


def test_rule_title_enabled_has_no_suffix():
    t = rule_title(_rule(enabled=True))
    assert t.endswith(" (disabled)") is False
    assert "08:30" in t


def test_rule_title_disabled_has_suffix():
    t = rule_title(_rule(enabled=False))
    assert t.endswith(" (disabled)")


def test_rule_title_label_branch_disabled():
    t = rule_title(_rule(label="Morning", enabled=False))
    assert t.startswith("Morning:")
    assert t.endswith(" (disabled)")


def test_rule_title_default_branch_disabled():
    r = _rule(level="default", action="optimizer", enabled=False)
    assert rule_title(r) == default_rule_title(r) + " (disabled)"