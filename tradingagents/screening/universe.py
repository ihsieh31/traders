"""Phase C universe: the full ACTIVE US_EQUITY asset list from Alpaca.

Deliberately NOT built on ``AlpacaUtils._get_searchable_assets`` /
``search_assets``: those exist for the UI symbol picker, are capped by a
search limit, cache briefly, and fall back to a static hot-stock list —
none of which may masquerade as the full-market universe. This module
queries the same trading client for every ACTIVE US_EQUITY asset
(consuming any server-side pagination), keeps only ``tradable=true``,
and fails loudly so a partial universe can never look like a scan.
"""

from __future__ import annotations

from typing import Any, List, Optional

from tradingagents.dataflows.alpaca_utils import get_alpaca_trading_client


class UniverseError(RuntimeError):
    """The full asset list could not be retrieved; the scan must stop."""


def normalize_symbol(symbol: Any) -> str:
    """Canonical US-equity symbol: uppercase, no whitespace or stray punctuation."""
    text = str(symbol or "").upper().strip().strip(",;")
    return "".join(text.split())


def enum_value(value: Any) -> str:
    """Lowercased underlying value for enum-or-string Alpaca fields.

    Real ``alpaca-py`` models expose ``Asset.status`` / ``Asset.asset_class``
    as enums where ``str(enum)`` is ``"AssetStatus.ACTIVE"`` (not ``"active"``).
    ``getattr(value, "value", value)`` accepts both real enums and plain-string
    fakes without changing the intended filters.
    """
    return str(getattr(value, "value", value) or "").lower()


def _consume_pages(client: Any, request: Any) -> List[Any]:
    """Materialize the asset list across any pagination the API/SDK uses.

    alpaca-py 0.44 returns the full list from one GET, but the service
    may paginate (page tokens): any ``next_page_token`` attribute on the
    response objects or a paged iterator is followed to exhaustion.
    """
    raw = client.get_all_assets(request)
    if raw is None:
        return []

    # Generator/iterator (paged SDK): consume fully.
    if not isinstance(raw, (list, tuple)) and hasattr(raw, "__iter__"):
        items = list(raw)
        return items if items else []

    items: List[Any] = list(raw)
    # Defensive page-token following for future SDK shapes.
    while items and hasattr(items[-1], "next_page_token"):
        last = items.pop()
        token = getattr(last, "next_page_token", None)
        if not token:
            break
        page = client.get_all_assets(request, page_token=token) if _accepts_page_token(client) else []
        items.extend(list(page or []))
    return items


def _accepts_page_token(client: Any) -> bool:
    try:
        import inspect

        return "page_token" in inspect.signature(client.get_all_assets).parameters
    except (TypeError, ValueError):
        return False


def fetch_us_equity_universe(broker: Optional[Any] = None) -> List[dict]:
    """Return every ACTIVE, tradable US_EQUITY asset as a compact dict.

    Raises :class:`UniverseError` on any failure — a degraded list must
    stop the scan, not shrink it silently.
    """
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    client = broker if broker is not None else get_alpaca_trading_client()
    request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    try:
        assets = _consume_pages(client, request)
    except UniverseError:
        raise
    except Exception as exc:
        raise UniverseError(f"Alpaca ACTIVE US_EQUITY asset list unavailable: {exc}") from exc

    universe: List[dict] = []
    seen = set()
    for asset in assets:
        symbol = normalize_symbol(getattr(asset, "symbol", ""))
        status = enum_value(getattr(asset, "status", ""))
        asset_class = enum_value(getattr(asset, "asset_class", ""))
        if not symbol or symbol in seen:
            continue
        if status != "active" or asset_class != AssetClass.US_EQUITY.value:
            continue
        if not bool(getattr(asset, "tradable", False)):
            continue
        seen.add(symbol)
        universe.append(
            {
                "symbol": symbol,
                "name": getattr(asset, "name", "") or symbol,
                "exchange": str(getattr(asset, "exchange", "") or ""),
            }
        )
    if not universe:
        raise UniverseError("Alpaca returned zero ACTIVE tradable US_EQUITY assets")
    universe.sort(key=lambda item: item["symbol"])
    return universe
