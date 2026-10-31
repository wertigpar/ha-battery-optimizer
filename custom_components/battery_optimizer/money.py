"""Locale-aware money helpers.

Pure module: imports no Home Assistant code, so it unit-tests without a running
instance. Everything locale-dependent about money lives here — which currency we
are in, and what the sensible starting values are for that locale.
"""

from __future__ import annotations

from dataclasses import dataclass

from .const import (
    _COUNTRY_VAT_MULTIPLIER,
    _CURRENCY_MONETARY_DEFAULTS,
    DEFAULT_SALES_COMMISSION,
    DEFAULT_TRANSFER_FEE_BUY,
    currency_for_timezone,
)

EUR = "EUR"
FI_COUNTRY = "FI"
FI_TIME_ZONE = "Europe/Helsinki"


def _norm(value: str | None) -> str:
    """Strip and uppercase a code, turning None into an empty string."""
    return (value or "").strip().upper()


def resolve_currency(
    *,
    override: str | None,
    ha_currency: str | None,
    time_zone: str,
) -> str:
    """Resolve the one currency this integration operates in.

    Precedence: explicit override, then Home Assistant's configured currency,
    then the timezone map, then EUR. Never raises.
    """
    override_norm = _norm(override)
    if override_norm:
        return override_norm
    ha_norm = _norm(ha_currency)
    if ha_norm:
        return ha_norm
    return currency_for_timezone(time_zone or "") or EUR


def is_finland(*, country: str | None, time_zone: str) -> bool:
    """Whether the install is Finnish, for jurisdiction-specific defaults.

    Country decides when present; timezone is the fallback. Currency is
    deliberately not consulted — EUR spans several unrelated grid markets.
    """
    country_norm = _norm(country)
    if country_norm:
        return country_norm == FI_COUNTRY
    return (time_zone or "") == FI_TIME_ZONE


def monetary_defaults(currency: str) -> dict[str, float]:
    """Monetary defaults for a currency, falling back to the EUR row.

    Returns a copy so callers cannot mutate the table.
    """
    row = _CURRENCY_MONETARY_DEFAULTS.get(_norm(currency))
    if row is None:
        row = _CURRENCY_MONETARY_DEFAULTS[EUR]
    return dict(row)


def jurisdiction_defaults(*, is_finland: bool) -> dict[str, float]:
    """Grid-fee defaults. Only Finland has a shipped value we can stand behind."""
    if is_finland:
        return {
            "transfer_fee_buy": DEFAULT_TRANSFER_FEE_BUY,
            "sales_commission": DEFAULT_SALES_COMMISSION,
        }
    return {"transfer_fee_buy": 0.0, "sales_commission": 0.0}


def vat_multiplier_default(*, country: str | None) -> float:
    """Standard VAT multiplier for a country; 1.0 when we have no rate for it.

    Returning 1.0 rather than the Finnish rate is deliberate: inventing a tax for
    a country we have no rate for is the failure this change exists to stop. It is
    also the right answer for users whose price sensor is already VAT-inclusive.
    """
    return _COUNTRY_VAT_MULTIPLIER.get(_norm(country), 1.0)


def is_tuned(currency: str) -> bool:
    """Whether this currency has a tuned monetary-default row."""
    return _norm(currency) in _CURRENCY_MONETARY_DEFAULTS


# Denomination heads name cents rather than a currency; asserting one would be a guess.
# Compared exactly against the parsed head, so an appearance elsewhere cannot veto a code.
_CENTS_HEADS = frozenset(
    {"snt", "cent", "cents", "c", "ct", "cts", "öre", "øre", "ore"}
)


def currency_from_unit(unit: str | None) -> str | None:
    """Extract an ISO code from a price unit like ``SEK/kWh``.

    Returns None when the unit names a denomination (cents) rather than a
    currency, when it is missing, or when it is not in ``<CODE>/kWh`` form.
    """
    text = (unit or "").strip().lower()
    if not text:
        return None
    head, separator, tail = text.partition("/")
    if not separator or tail.strip() != "kwh":
        return None
    code = head.strip()
    if code in _CENTS_HEADS:
        return None
    code = code.upper()
    if len(code) == 3 and code.isascii() and code.isalpha():
        return code
    return None


@dataclass(frozen=True)
class LocaleMoney:
    """Every locale-derived money value this integration needs, resolved once."""

    currency: str
    vat_multiplier: float
    transfer_fee_buy: float
    sales_commission: float
    battery_wear_cost: float
    precharge_price_ceiling: float
    forced_sell_min_profit: float


def locale_money(
    *,
    override: str | None,
    ha_currency: str | None,
    time_zone: str,
    country: str | None,
) -> LocaleMoney:
    """Resolve currency plus all monetary defaults for one install."""
    currency = resolve_currency(
        override=override, ha_currency=ha_currency, time_zone=time_zone
    )
    monetary = monetary_defaults(currency)
    jurisdiction = jurisdiction_defaults(
        is_finland=is_finland(country=country, time_zone=time_zone)
    )
    return LocaleMoney(
        currency=currency,
        vat_multiplier=vat_multiplier_default(country=country),
        transfer_fee_buy=jurisdiction["transfer_fee_buy"],
        sales_commission=jurisdiction["sales_commission"],
        battery_wear_cost=monetary["battery_wear_cost"],
        precharge_price_ceiling=monetary["precharge_price_ceiling"],
        forced_sell_min_profit=monetary["forced_sell_min_profit"],
    )


# The values this integration shipped before locale awareness existed. A stored
# value that still matches one of these, in an install whose locale default
# differs, is strong evidence the field was never reviewed.
ORIGIN_LITERALS: dict[str, float] = {
    "transfer_fee_buy": 0.0776,
    "sales_commission": 0.003,
    "battery_wear_cost": 0.03,
    "precharge_price_ceiling": 0.07,
    "forced_sell_min_profit": 0.02,
    "vat_multiplier": 1.255,
}

_MONEY_EPSILON = 1e-9


def should_warn_mismatch(
    *, sensor_unit: str | None, currency: str, override: str | None
) -> bool:
    """Whether the price sensor's unit contradicts the currency in use.

    Validation only. A cents-form or unrecognised unit never warns, and an
    explicit override silences the warning because the user has answered it.
    """
    if _norm(override):
        return False
    unit_currency = currency_from_unit(sensor_unit)
    if unit_currency is None:
        return False
    return unit_currency != _norm(currency)


def unreviewed_fields(stored: dict, locale: LocaleMoney) -> list[str]:
    """Fields still holding a pre-locale default that is wrong for this install."""
    current = {
        "transfer_fee_buy": locale.transfer_fee_buy,
        "sales_commission": locale.sales_commission,
        "battery_wear_cost": locale.battery_wear_cost,
        "precharge_price_ceiling": locale.precharge_price_ceiling,
        "forced_sell_min_profit": locale.forced_sell_min_profit,
        "vat_multiplier": locale.vat_multiplier,
    }
    flagged: list[str] = []
    for key, origin in reversed(list(ORIGIN_LITERALS.items())):
        if key not in stored:
            continue
        try:
            value = float(stored[key])
        except (TypeError, ValueError):
            continue
        if (
            abs(value - origin) < _MONEY_EPSILON
            and abs(current[key] - origin) > _MONEY_EPSILON
        ):
            flagged.append(key)
    return flagged
