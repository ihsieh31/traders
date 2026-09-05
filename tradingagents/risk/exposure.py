"""Phase B deterministic exposure caps: one evaluator, real callers only.

Computes the effective opening notional for exposure-adding orders from one
authoritative broker snapshot. The canonical per-symbol cap stays
``safety.max_symbol_concentration_pct`` (no duplicate switch exists), the
sector cap is ``max_sector_exposure_pct``, and the gross cap is
``portfolio_max_gross_exposure_pct``.

Headroom (per-symbol, mirrors the implementation contract):

    available = max(0, equity * cap/100
                       - abs(current market value)
                       - outstanding increasing notional)

The submitted notional is min(proposed, symbol headroom, sector headroom,
gross headroom, cash). Quantity-based outstanding orders are estimated from
the execution quote; when an outstanding order's notional cannot be
reliably estimated the evaluator refuses the increase instead of guessing
zero. Verified risk-reducing exits never route through here (Phase A rules
govern them); a position flip clips only the increasing leg, with the
verified close's freed value credited deterministically from the snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Broker order statuses that can still add exposure. Anything terminal
# (filled/canceled/rejected/expired) consumes no headroom; unknown-but-live
# statuses stay conservative and count.
_LIVE_ORDER_STATUSES_EXCLUSIVE = ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")

# Alpaca minimum order notional; below this an order is not placeable.
MIN_ORDER_NOTIONAL = 1.0


@dataclass(frozen=True)
class ExposureDecision:
    approved: bool
    notional: float
    reason: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "notional": self.notional,
            "reason": self.reason,
            **self.details,
        }


def _is_live(status: str) -> bool:
    value = str(status or "").lower()
    return not any(marker in value for marker in _LIVE_ORDER_STATUSES_EXCLUSIVE)


def outstanding_increasing_notional(
    snapshot: Any,
    *,
    symbols: Optional[set[str]] = None,
    reference_price: Optional[float] = None,
) -> tuple[float, bool]:
    """Sum live exposure-adding open-order notional from the snapshot.

    A buy order adds long exposure. A sell order adds exposure only for a
    currently short position (a sell against a long position reduces it).
    Returns (total, fully_estimated): ``fully_estimated`` is False when any
    counted order's value had to be guessed, which callers must treat as
    "refuse the increase" rather than trusting the partial sum.
    """
    total = 0.0
    fully_estimated = True
    for order in snapshot.orders:
        if not _is_live(order.status):
            continue
        if symbols is not None and order.symbol not in symbols:
            continue
        position = snapshot.position(order.symbol)
        short_side = position is not None and position.qty < 0
        if str(order.side).lower() == "buy":
            increasing = True
        elif str(order.side).lower() == "sell" and short_side:
            increasing = True
        else:
            continue
        if not increasing:
            continue
        if order.notional is not None and order.notional > 0:
            total += float(order.notional)
            continue
        qty = float(order.qty or 0) - float(order.filled_qty or 0)
        if qty <= 0:
            continue
        if reference_price and reference_price > 0:
            total += qty * float(reference_price)
        else:
            fully_estimated = False
    return total, fully_estimated


def _rejection(reason: str, details: Optional[dict] = None) -> ExposureDecision:
    return ExposureDecision(approved=False, notional=0.0, reason=reason, details=details or {})


def evaluate_opening_exposure(
    *,
    symbol: str,
    proposed_notional: float,
    snapshot: Any,
    quote_price: Optional[float] = None,
    symbol_cap_pct: float = 25.0,
    sector_cap_pct: Optional[float] = 30.0,
    gross_cap_pct: Optional[float] = 100.0,
    sector_mapping: Optional[dict] = None,
    planned_close_reduction: float = 0.0,
) -> ExposureDecision:
    """Clip a proposed opening notional to the most binding deterministic cap.

    ``planned_close_reduction`` credits the dollar value freed by a verified
    close leg in the same intent (close-then-open flip) so only the truly
    increasing part is clipped; verified reducing exits themselves never
    pass through this evaluator.
    """
    sector_mapping = dict(sector_mapping or {})
    normalized = (symbol or "").upper().replace("/", "")

    # A positive, explicit per-symbol cap is required for automatic
    # execution. The legacy 0/"uncapped" value is a configuration error here
    # rather than an unbounded book.
    if symbol_cap_pct is None or symbol_cap_pct <= 0:
        return _rejection(
            "max_symbol_concentration_pct must be a positive percentage for "
            "automatic execution (0/uncapped is not permitted); refusing to add risk",
            {"cap": symbol_cap_pct},
        )

    if gross_cap_pct is not None and gross_cap_pct <= 0:
        return _rejection(
            f"portfolio_max_gross_exposure_pct must be positive when set, got {gross_cap_pct}",
            {"cap": gross_cap_pct},
        )

    try:
        proposed = float(proposed_notional)
    except (TypeError, ValueError):
        return _rejection(f"invalid proposed notional: {proposed_notional!r}")
    if proposed != proposed or proposed in (float("inf"), float("-inf")):
        return _rejection(f"invalid proposed notional: {proposed_notional!r}")
    if proposed <= 0:
        return _rejection(f"proposed notional must be positive, got {proposed}")

    equity = float(snapshot.equity)
    position = snapshot.position(normalized)
    if planned_close_reduction and position is not None:
        # Only a full verified close frees its whole market value; a partial
        # close frees proportionally.
        total_close = float(planned_close_reduction)
        position_value = abs(float(position.market_value))
        freed = min(total_close, position_value)
    else:
        freed = 0.0
    current_symbol_value = max(
        0.0,
        (abs(float(position.market_value)) if position is not None else 0.0) - freed,
    )

    symbol_outstanding, symbol_estimated = outstanding_increasing_notional(
        snapshot,
        symbols={normalized},
        reference_price=quote_price,
    )

    # --- sector accounting (fail closed on unknown mappings) ---
    # The sector cap is enabled when the operator provides a sector_mapping
    # (a cap without any mapping could never be proven). The cap value
    # defaults to 30% and must satisfy 0 < cap <= 100 whenever enabled; a
    # provided mapping with an invalid cap is a configuration error.
    sector_exposure_used = 0.0
    sector_outstanding = 0.0
    sector_estimated = True
    sector_cap_applicable = bool(sector_mapping) and sector_cap_pct is not None
    symbol_sector: Optional[str] = None
    if sector_cap_applicable and not (0 < sector_cap_pct <= 100):
        return _rejection(
            f"max_sector_exposure_pct must satisfy 0 < cap <= 100, got {sector_cap_pct}",
            {"cap": sector_cap_pct},
        )
    if sector_cap_applicable:
        symbol_sector = sector_mapping.get(normalized)
        if symbol_sector is None:
            return _rejection(
                f"unknown sector for {normalized}: add it to sector_mapping "
                "before adding exposure (sector cap cannot be proven)",
                {"missing_mapping": normalized},
            )
        held_sectors: dict[str, str] = {}
        for held in snapshot.positions:
            held_sector = sector_mapping.get(held.symbol)
            if held_sector is None:
                return _rejection(
                    f"unknown sector for existing holding {held.symbol}: add it "
                    "to sector_mapping (sector cap cannot be proven)",
                    {"missing_mapping": held.symbol},
                )
            held_sectors[held.symbol] = held_sector
            if held_sector == symbol_sector:
                sector_exposure_used += abs(float(held.market_value))

        same_sector_symbols = {
            sym for sym, sec in held_sectors.items() if sec == symbol_sector
        }
        same_sector_symbols.add(normalized)
        sector_outstanding, sector_estimated = outstanding_increasing_notional(
            snapshot,
            symbols=same_sector_symbols,
            reference_price=quote_price,
        )

    all_outstanding, all_estimated = outstanding_increasing_notional(
        snapshot, reference_price=quote_price
    )

    if not symbol_estimated:
        return _rejection(
            f"cannot reliably estimate the notional of an outstanding buy "
            f"order on {normalized} (no notional and no reference price); "
            "refusing to add risk instead of reusing headroom",
        )
    if sector_cap_applicable and not sector_estimated:
        return _rejection(
            f"cannot reliably estimate outstanding order notional for sector "
            f"'{symbol_sector}'; refusing to add risk",
        )
    if not all_estimated:
        return _rejection(
            "cannot reliably estimate outstanding order notional for the "
            "gross/cash check; refusing to add risk",
        )

    symbol_headroom = max(
        0.0, equity * float(symbol_cap_pct) / 100.0 - current_symbol_value - symbol_outstanding
    )
    limits = {
        "symbol_headroom": symbol_headroom,
        "symbol_outstanding": symbol_outstanding,
        "current_symbol_value": current_symbol_value,
        "freed_by_verified_close": freed,
    }

    if sector_cap_applicable:
        sector_headroom = max(
            0.0,
            equity * float(sector_cap_pct) / 100.0
            - sector_exposure_used
            - sector_outstanding,
        )
        limits["sector"] = symbol_sector
        limits["sector_headroom"] = sector_headroom
        limits["sector_exposure_used"] = sector_exposure_used
        limits["sector_outstanding"] = sector_outstanding
    else:
        sector_headroom = float("inf")

    if gross_cap_pct is not None:
        gross_headroom = max(
            0.0,
            equity * float(gross_cap_pct) / 100.0
            - float(snapshot.gross_exposure)
            - all_outstanding,
        )
        limits["gross_headroom"] = gross_headroom
    else:
        gross_headroom = float("inf")

    cash_limit = max(0.0, float(snapshot.cash) - all_outstanding)
    limits["cash_limit"] = cash_limit
    limits["gross_outstanding"] = all_outstanding

    effective = min(proposed, symbol_headroom, sector_headroom, gross_headroom, cash_limit)
    limits["proposed_notional"] = proposed
    limits["effective_notional"] = effective

    if effective < MIN_ORDER_NOTIONAL:
        binding = min(
            (symbol_headroom, "symbol cap"),
            (sector_headroom, "sector cap"),
            (gross_headroom, "gross cap"),
            (cash_limit, "available cash"),
        )[1]
        return _rejection(
            f"no placeable opening notional after caps: effective "
            f"${effective:,.2f} is below the ${MIN_ORDER_NOTIONAL:,.2f} minimum "
            f"(binding: {binding})",
            limits,
        )

    clipped = round(effective, 2)
    limits["clipped"] = clipped < proposed
    return ExposureDecision(
        approved=True,
        notional=clipped,
        reason=(
            f"Opening notional clipped to ${clipped:,.2f}"
            if clipped < proposed
            else f"Opening notional ${clipped:,.2f} within all caps"
        ),
        details=limits,
    )
