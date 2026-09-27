"""Small deterministic financial-rigor helpers for Berkshire analysis."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("numeric value is required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("numeric value must be finite")
    return result


def verify_market_cap(*, price: Any, shares_outstanding: Any, reported_market_cap: Any | None = None) -> dict[str, str]:
    price, shares = _decimal(price), _decimal(shares_outstanding)
    if price <= 0 or shares <= 0:
        raise ValueError("price and shares_outstanding must be positive")
    implied = price * shares
    result = {"implied_market_cap": str(implied)}
    if reported_market_cap is not None:
        reported = _decimal(reported_market_cap)
        if reported <= 0:
            raise ValueError("reported_market_cap must be positive")
        result["reported_market_cap"] = str(reported)
        result["difference"] = str(implied - reported)
    return result


def cross_validate(*, primary: Any, secondary: Any, tolerance: Any = "0.05") -> dict[str, Any]:
    left = _decimal(primary)
    right = _decimal(secondary)
    scale = max(abs(left), abs(right), Decimal("1"))
    relative_difference = abs(left - right) / scale
    allowed = _decimal(tolerance)
    if not 0 <= allowed <= 1:
        raise ValueError("tolerance must be between 0 and 1")
    return {
        "primary": str(left),
        "secondary": str(right),
        "relative_difference": str(relative_difference),
        "within_tolerance": relative_difference <= allowed,
    }


def verify_valuation(*, enterprise_value: Any, revenue: Any, earnings: Any | None = None) -> dict[str, str]:
    ev = _decimal(enterprise_value)
    rev = _decimal(revenue)
    result = {"ev_to_revenue": str(ev / rev) if rev else "unavailable"}
    if earnings is not None:
        earn = _decimal(earnings)
        result["ev_to_earnings"] = str(ev / earn) if earn else "unavailable"
    return result
