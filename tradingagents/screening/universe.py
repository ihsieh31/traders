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

import fcntl
import json
import logging
import math
import os
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, List, Optional

from tradingagents.app_identity import app_home
from tradingagents.dataflows.alpaca_utils import (
    fetch_with_bounded_retry,
    get_alpaca_trading_client,
)


class UniverseError(RuntimeError):
    """The full asset list could not be retrieved; the scan must stop."""


_NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
_NASDAQ_REQUEST_TIMEOUT_SECONDS = 20
_NASDAQ_MAX_RESPONSE_BYTES = 20_000_000
_NASDAQ_CACHE_VERSION = 1
_LOG = logging.getLogger(__name__)


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
    raw = fetch_with_bounded_retry(lambda: client.get_all_assets(request))
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
        page = fetch_with_bounded_retry(
            lambda: client.get_all_assets(request, page_token=token)
        ) if _accepts_page_token(client) else []
        items.extend(list(page or []))
    return items


def _accepts_page_token(client: Any) -> bool:
    try:
        import inspect

        return "page_token" in inspect.signature(client.get_all_assets).parameters
    except (TypeError, ValueError):
        return False


def _market_cap_cache_dir() -> Path:
    return app_home() / "cache" / "market_cap"


def _parse_market_cap(value: Any) -> Optional[float]:
    """Parse Nasdaq's USD market-cap field without accepting unusable values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str):
            # Nasdaq currently returns a decimal number with optional grouping
            # commas. A dollar sign is accepted as formatting, never as a unit.
            value = value.strip().replace(",", "").replace("$", "")
            if not value:
                return None
        market_cap = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(market_cap) or market_cap <= 0:
        return None
    return market_cap


def _download_nasdaq_stock_data() -> dict:
    """Download the Nasdaq screener's all-exchange Symbol + Market Cap data."""
    query = urllib.parse.urlencode(
        {"tableonly": "true", "limit": 10_000, "offset": 0, "exchange": "all"}
    )
    request = urllib.request.Request(
        f"{_NASDAQ_SCREENER_URL}?{query}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
            "User-Agent": "Mozilla/5.0 (compatible; tradingBuffett/1.0)",
        },
    )
    with urllib.request.urlopen(
        request, timeout=_NASDAQ_REQUEST_TIMEOUT_SECONDS
    ) as response:
        raw = response.read(_NASDAQ_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _NASDAQ_MAX_RESPONSE_BYTES:
        raise ValueError("Nasdaq screener response exceeded the size limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Nasdaq screener response was not a JSON object")
    return payload


def _market_caps_from_nasdaq_payload(payload: dict) -> tuple[dict[str, float], int]:
    data = payload.get("data")
    table = data.get("table") if isinstance(data, dict) else None
    rows = table.get("rows") if isinstance(table, dict) else None
    total_records = data.get("totalrecords") if isinstance(data, dict) else None
    if not isinstance(rows, list) or isinstance(total_records, bool):
        raise ValueError("Nasdaq screener response is missing complete stock rows")
    try:
        total_records = int(total_records)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Nasdaq screener response has an invalid row count") from exc
    if total_records <= 0 or len(rows) != total_records:
        raise ValueError("Nasdaq screener response did not contain all stock rows")

    market_caps: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol"))
        market_cap = _parse_market_cap(row.get("marketCap"))
        if symbol and market_cap is not None:
            market_caps[symbol] = market_cap
    if not market_caps:
        raise ValueError("Nasdaq screener response contained no valid market caps")
    return market_caps, total_records


def _read_market_cap_cache(path: Path, *, today: date) -> Optional[dict[str, float]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _NASDAQ_CACHE_VERSION
            or payload.get("source") != "nasdaq_screener"
            or payload.get("date") != today.isoformat()
            or payload.get("row_count") != payload.get("total_records")
            or isinstance(payload.get("row_count"), bool)
            or not isinstance(payload.get("row_count"), int)
            or payload["row_count"] <= 0
            or not isinstance(payload.get("market_caps"), dict)
        ):
            return None
        market_caps = {}
        for raw_symbol, raw_cap in payload["market_caps"].items():
            symbol = normalize_symbol(raw_symbol)
            cap = _parse_market_cap(raw_cap)
            if symbol and cap is not None:
                market_caps[symbol] = cap
        return market_caps or None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _write_market_cap_cache(path: Path, payload: dict) -> None:
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def fetch_nasdaq_market_caps(
    *, cache_dir: Optional[Path] = None, today: Optional[date] = None
) -> dict[str, float]:
    """Return daily-cached Nasdaq market caps with bounded transient retries.

    Missing data returns an empty mapping for the fail-closed market-cap gate.
    """
    today = today or date.today()
    cache_dir = Path(cache_dir) if cache_dir is not None else _market_cap_cache_dir()
    cache_path = cache_dir / f"nasdaq_{today.isoformat()}.json"
    lock_path = cache_dir / ".nasdaq_market_cap.lock"

    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            cached = _read_market_cap_cache(cache_path, today=today)
            if cached is not None:
                return cached
            try:
                payload = fetch_with_bounded_retry(_download_nasdaq_stock_data)
                market_caps, total_records = _market_caps_from_nasdaq_payload(payload)
            except Exception as exc:
                _LOG.warning("Nasdaq market-cap download unavailable: %s", exc)
                return {}
            cache_payload = {
                "version": _NASDAQ_CACHE_VERSION,
                "source": "nasdaq_screener",
                "date": today.isoformat(),
                "row_count": total_records,
                "total_records": total_records,
                "market_caps": market_caps,
            }
            try:
                _write_market_cap_cache(cache_path, cache_payload)
            except OSError as exc:
                _LOG.warning("Could not persist Nasdaq market-cap cache: %s", exc)
            return market_caps
    except OSError as exc:
        _LOG.warning("Nasdaq market-cap cache unavailable: %s", exc)
        return {}


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
    market_caps = fetch_nasdaq_market_caps()
    for item in universe:
        item["market_cap"] = market_caps.get(item["symbol"])
    return universe
