"""Currency field must not break the flow schema (issue #37).

Home Assistant converts every flow schema to JSON for the frontend with
``voluptuous_serialize``.  ``vol.Match`` has no converter, so a currency field
validated with it made the add and Configure forms fail with a 500 on
Home Assistant 2026.9.  ``test_options_schema_serialises`` covers the real
conversion but is skipped where Home Assistant is not installed, so these
tests check the source directly and need no Home Assistant.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

SRC = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "battery_optimizer"
    / "config_flow.py"
)

# voluptuous markers voluptuous_serialize can convert.  Anything else used in
# a flow schema (Match, Length, Any, Email, ...) has no converter, or only a
# partial one, and breaks the form.
SERIALISABLE = {"Schema", "Required", "Optional", "In", "Coerce", "Range", "All"}


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _vol_attr(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "vol"
    ):
        return node.attr
    return None


def test_flow_schema_uses_only_serialisable_validators():
    used = {a for a in map(_vol_attr, ast.walk(_tree())) if a}
    used.discard("Any")  # allowed only in its two-member form, checked below
    assert used <= SERIALISABLE, f"unserialisable voluptuous validators: {used - SERIALISABLE}"


def test_vol_any_only_as_optional_wrapper():
    """probatio keeps the widget only for ``Any(None, x)``; wider ones raise."""
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and _vol_attr(node.func) == "Any":
            args = node.args
            none_members = [
                a for a in args if isinstance(a, ast.Constant) and a.value is None
            ]
            assert len(args) == 2 and len(none_members) == 1, (
                f"vol.Any at line {node.lineno} must be Any(None, x)"
            )


def _load_currency_error():
    tree = _tree()
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_currency_error"
    )
    pattern = next(
        n for n in tree.body
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", "") == "_CURRENCY_CODE"
    )
    ns: dict[str, Any] = {"re": re, "Any": Any, "CONF_CURRENCY": "currency"}
    exec(compile(ast.Module([pattern, fn], []), str(SRC), "exec"), ns)  # noqa: S102
    return ns["_currency_error"]


@pytest.fixture(scope="module")
def currency_error():
    return _load_currency_error()


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_means_no_override(currency_error, value):
    data = {"currency": value}
    assert currency_error(data) is None
    assert "currency" not in data


def test_missing_key_is_fine(currency_error):
    data: dict = {}
    assert currency_error(data) is None
    assert data == {}


@pytest.mark.parametrize("value,stored", [("sek", "SEK"), ("NOK", "NOK"), (" dkk ", "DKK")])
def test_valid_code_is_stored_upper_case(currency_error, value, stored):
    data = {"currency": value}
    assert currency_error(data) is None
    assert data["currency"] == stored


@pytest.mark.parametrize("value", ["EURO", "EU", "12$", "E U", "€", "SE1"])
def test_bad_code_is_rejected(currency_error, value):
    data = {"currency": value}
    assert currency_error(data) == "invalid_currency"
    assert data["currency"] == value  # left untouched for the re-shown form


def test_error_text_exists_in_every_language():
    import json

    base = SRC.parent
    for name in ("strings.json", *(f"translations/{c}.json" for c in ("en", "fi", "sv", "da", "nb"))):
        data = json.loads((base / name).read_text(encoding="utf-8"))
        assert data["config"]["error"]["invalid_currency"], name
        assert data["options"]["error"]["invalid_currency"], name
