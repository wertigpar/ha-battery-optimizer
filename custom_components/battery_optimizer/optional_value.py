"""Pure helpers for optional form values.

No Home Assistant imports and no relative imports - unit-tested directly in
the repo's local tests/ directory.
"""

from __future__ import annotations


def is_blank(value: object) -> bool:
    """True when a submitted value means "the user left this field empty".

    A blank is ``None`` or a string that is empty or whitespace-only.  Every
    other value is a real value, including ``0`` and ``False``: the next-day
    price publish hour accepts 0 (midnight), so treating a zero as blank
    would silently discard a legitimate setting.
    """
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


__all__ = ["is_blank"]
