"""Optional-value blank detection.

Pure-module tests (optional_value.py): no HA imports, no relative imports.

``is_blank`` decides what "the user left this field empty" means for an
optional numeric field.  It must not treat a legitimate zero as blank - the
publish hour 0 is midnight, a real value.
"""

from __future__ import annotations

from . import load_optional_value

optional_value, const = load_optional_value()
is_blank = optional_value.is_blank


def test_none_is_blank():
    assert is_blank(None) is True


def test_empty_string_is_blank():
    assert is_blank("") is True


def test_whitespace_only_string_is_blank():
    assert is_blank("   ") is True
    assert is_blank("\t\n") is True


def test_zero_is_not_blank():
    """0 is a legitimate publish hour (midnight), not an empty field."""
    assert is_blank(0) is False
    assert is_blank(0.0) is False


def test_false_is_not_blank():
    """False is a value, not a blank - booleans are ints in Python."""
    assert is_blank(False) is False


def test_numbers_are_not_blank():
    assert is_blank(14) is False
    assert is_blank(14.0) is False


def test_numeric_strings_are_not_blank():
    assert is_blank("14") is False
    assert is_blank(" 14 ") is False
    assert is_blank("0") is False


def test_non_numeric_string_is_not_blank():
    """A typo is an error, not an empty field - it must still be rejected."""
    assert is_blank("abc") is False
    assert is_blank("14h") is False
