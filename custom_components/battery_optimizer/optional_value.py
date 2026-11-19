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


def form_default(value: object) -> dict:
    """Schema kwargs for an optional form field whose value may be unset.

    Home Assistant serialises ``vol.Optional(..., default=None)`` to a field
    with ``"default": null``, and the frontend then drops the field entirely
    (frontend#26101).  An unset value therefore must contribute NO default,
    while a real value is offered as ``suggested_value`` so it still pre-fills
    without becoming a hard default.

    Usage: ``vol.Optional(KEY, **form_default(value))``.
    """
    if value is None:
        return {}
    return {"description": {"suggested_value": value}}


__all__ = ["is_blank", "form_default"]
