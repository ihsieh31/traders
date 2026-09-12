# alpaca_utils.py

import math
import os
import pandas as pd
import time
from functools import partial
from datetime import datetime, timedelta
from typing import Annotated, Union, Optional, List, Dict, Any, TYPE_CHECKING
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest, StockLatestQuoteRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetAssetsRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import AssetClass, AssetStatus, QueryOrderStatus
from alpaca.common.enums import Sort
from .config import get_api_key, get_alpaca_use_paper, get_config
from .ticker_utils import TickerUtils
# Imported lazily inside execute_trade_intent: a module-level import would
# create a circular import (dataflows -> agents -> dataflows.interface).
if TYPE_CHECKING:
    from tradingagents.agents.schemas import TradeIntent

from tradingagents.risk.position_sizing import (
    PositionSizer,
    RiskParameters,
    SizingDecision,
    compute_atr,
)


class AlpacaAccountDataError(RuntimeError):
    """Public-safe account-data failure for the WebUI.

    Broker exceptions can contain request details or credentials.  Preserve
    the original exception only through chaining for internal diagnostics;
    the public message deliberately contains the resource and exception type,
    never the raw exception text.
    """

    def __init__(self, resource: str, cause: BaseException):
        super().__init__(f"Alpaca {resource} unavailable ({type(cause).__name__})")


# Fallback dictionary for company names
ticker_to_company_fallback = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Google",
    "AMZN": "Amazon",
    "TSLA": "Tesla",
    "NVDA": "Nvidia",
    "TSM": "Taiwan Semiconductor Manufacturing Company OR TSMC",
    "JPM": "JPMorgan Chase OR JP Morgan",
    "JNJ": "Johnson & Johnson OR JNJ",
    "V": "Visa",
    "WMT": "Walmart",
    "META": "Meta OR Facebook",
    "AMD": "AMD",
    "INTC": "Intel",
    "QCOM": "Qualcomm",
    "BABA": "Alibaba",
    "ADBE": "Adobe",
    "NFLX": "Netflix",
    "CRM": "Salesforce",
    "PYPL": "PayPal",
    "VZ": "Verizon OR Verizon Communications",
    "PLTR": "Palantir",
    "MU": "Micron",
    "SQ": "Block OR Square",
    "ZM": "Zoom",
    "CSCO": "Cisco",
    "SHOP": "Shopify",
    "ORCL": "Oracle",
    "X": "Twitter OR X",
    "SPOT": "Spotify",
    "AVGO": "Broadcom",
    "ASML": "ASML ",
    "TWLO": "Twilio",
    "SNAP": "Snap Inc.",
    "TEAM": "Atlassian",
    "SQSP": "Squarespace",
    "UBER": "Uber",
    "ROKU": "Roku",
    "PINS": "Pinterest",
}


_ASSET_SEARCH_CACHE = {
    "expires_at": 0.0,
    "assets": [],
}


def _enum_value(value) -> str:
    return getattr(value, "value", value) or ""


def _normalize_crypto_symbol(symbol: str) -> str:
    raw = (symbol or "").upper().replace("-", "/")
    if "/" in raw:
        return raw
    for quote in ("USDT", "USDC", "USD"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return f"{raw[:-len(quote)]}/{quote}"
    return raw


def _normalize_asset_symbol(symbol: str, asset_class: str) -> str:
    raw = (symbol or "").upper()
    if asset_class == AssetClass.CRYPTO.value:
        return _normalize_crypto_symbol(raw)
    return raw


def _asset_to_search_result(asset) -> Dict[str, Any]:
    asset_class = _enum_value(getattr(asset, "asset_class", "")) or "unknown"
    symbol = _normalize_asset_symbol(getattr(asset, "symbol", ""), asset_class)
    name = getattr(asset, "name", "") or ticker_to_company_fallback.get(symbol, symbol)
    exchange = _enum_value(getattr(asset, "exchange", "")) or ""
    tradable = bool(getattr(asset, "tradable", False))
    asset_type = "Crypto" if asset_class == AssetClass.CRYPTO.value else "Equity"
    return {
        "symbol": symbol,
        "name": name,
        "asset_class": asset_class,
        "asset_type": asset_type,
        "exchange": exchange,
        "tradable": tradable,
        "market_cap": None,
    }


def _fallback_asset_results() -> List[Dict[str, Any]]:
    stocks = [
        ("NVDA", "NVIDIA Corporation", "NASDAQ"),
        ("AMD", "Advanced Micro Devices, Inc.", "NASDAQ"),
        ("TSLA", "Tesla, Inc.", "NASDAQ"),
        ("AAPL", "Apple Inc.", "NASDAQ"),
        ("MSFT", "Microsoft Corporation", "NASDAQ"),
    ]
    crypto = [
        ("BTC/USD", "Bitcoin / US Dollar"),
        ("ETH/USD", "Ethereum / US Dollar"),
        ("SOL/USD", "Solana / US Dollar"),
    ]
    results = [
        {
            "symbol": symbol,
            "name": name,
            "asset_class": AssetClass.US_EQUITY.value,
            "asset_type": "Equity",
            "exchange": exchange,
            "tradable": True,
            "market_cap": None,
        }
        for symbol, name, exchange in stocks
    ]
    results.extend(
        {
            "symbol": symbol,
            "name": name,
            "asset_class": AssetClass.CRYPTO.value,
            "asset_type": "Crypto",
            "exchange": "Alpaca Crypto",
            "tradable": True,
            "market_cap": None,
        }
        for symbol, name in crypto
    )
    return results


def _apply_read_timeout(client, timeout=(3.05, 10.0)):
    """Wrap the client's requests session with fixed connect/read timeouts.

    The execution TradingClient already owns this pattern; historical data
    clients get the same finite bound so a stalled market-data GET cannot
    freeze a synchronous loop indefinitely. Returns True when the installed
    alpaca-py shape allowed enforcement, False otherwise.
    """
    session = getattr(client, "_session", None)
    request = getattr(session, "request", None)
    if session is None or not callable(request):
        return False
    session.request = partial(request, timeout=timeout)
    return True


def get_alpaca_stock_client() -> StockHistoricalDataClient:
    api_key = get_api_key("alpaca_api_key", "ALPACA_API_KEY")
    api_secret = get_api_key("alpaca_secret_key", "ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        print(f"Warning: Missing Alpaca API credentials. API key: {'present' if api_key else 'missing'}, Secret: {'present' if api_secret else 'missing'}")
        raise ValueError("Alpaca API key or secret not found. Please set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
    try:
        client = StockHistoricalDataClient(api_key, api_secret)
    except Exception as e:
        print(f"Error creating Alpaca stock client: {e}")
        raise
    if not _apply_read_timeout(client):
        # A historical client without an enforceable timeout can stall the
        # synchronous screening/analysis loop forever; refuse it instead.
        raise RuntimeError(
            "Installed alpaca-py cannot enforce finite historical-data timeouts"
        )
    return client


def get_alpaca_crypto_client() -> CryptoHistoricalDataClient:
    api_key = get_api_key("alpaca_api_key", "ALPACA_API_KEY")
    api_secret = get_api_key("alpaca_secret_key", "ALPACA_SECRET_KEY")
    # Crypto calls work without keys, but keys raise rate limits
    if api_key and api_secret:
        client = CryptoHistoricalDataClient(api_key, api_secret)
    else:
        client = CryptoHistoricalDataClient()
    if not _apply_read_timeout(client):
        raise RuntimeError(
            "Installed alpaca-py cannot enforce finite crypto-historical timeouts"
        )
    return client


PAPER_API_BASE_URL = "https://paper-api.alpaca.markets"


class PaperTradingEnforcementError(RuntimeError):
    """Raised when any live/non-paper trading path is attempted (fail closed)."""


def _resolve_paper_base_url(explicit_base_url: Optional[str] = None) -> Optional[str]:
    import os as _os

    candidate = explicit_base_url
    if candidate is None:
        candidate = _os.getenv("ALPACA_BASE_URL") or _os.getenv("ALPACA_PAPER_BASE_URL")
    if candidate is None:
        return None
    return str(candidate).strip()


def validate_paper_endpoint(base_url: Optional[str]) -> None:
    """Fail closed unless the endpoint is explicitly the Alpaca Paper API.

    None (SDK default paper endpoint via paper=True) is allowed. Any other
    URL must exactly match the paper endpoint; live or unknown hosts raise.
    """
    if base_url is None or str(base_url).strip() == "":
        return
    normalized = str(base_url).strip().rstrip("/")
    paper = PAPER_API_BASE_URL.rstrip("/")
    if normalized == paper or normalized.startswith(paper + "/"):
        return
    raise PaperTradingEnforcementError(
        f"Refusing non-paper Alpaca endpoint: {base_url!r}. "
        f"Paper-only mode allows exactly {PAPER_API_BASE_URL}."
    )


def get_alpaca_trading_client(base_url: Optional[str] = None) -> TradingClient:
    """Paper-only trading/account/order client factory (sole production factory).

    Always constructs TradingClient(..., paper=True). ALPACA_USE_PAPER no
    longer controls execution: an explicit false-like value fails closed
    instead of opening a live path. Unknown/live base URLs fail closed.
    Market-data clients are intentionally untouched by this gate.
    """
    api_key = get_api_key("alpaca_api_key", "ALPACA_API_KEY")
    api_secret = get_api_key("alpaca_secret_key", "ALPACA_SECRET_KEY")
    if not api_key or not api_secret:
        raise ValueError("Alpaca API key or secret not found. Please set ALPACA_API_KEY and ALPACA_SECRET_KEY.")
    raw_paper_flag = get_alpaca_use_paper()
    if isinstance(raw_paper_flag, bool):
        flag_text = "true" if raw_paper_flag else "false"
    else:
        flag_text = str(raw_paper_flag if raw_paper_flag is not None else "True").strip().lower()
    if flag_text in ("false", "0", "no", "off", "live"):
        raise PaperTradingEnforcementError(
            "ALPACA_USE_PAPER=False is not supported: this build is paper-only. "
            "Remove the live setting (use paper keys) instead of switching endpoints."
        )
    resolved_base = _resolve_paper_base_url(base_url)
    validate_paper_endpoint(resolved_base)
    kwargs: Dict[str, Any] = {"paper": True}
    # Only forward an explicit, validated paper override; otherwise rely on
    # the SDK default paper endpoint for paper=True.
    if resolved_base:
        kwargs["url_override"] = resolved_base
    try:
        client = TradingClient(api_key, api_secret, **kwargs)
    except TypeError:
        # Older alpaca-py without url_override: only the default paper
        # endpoint exists, which is exactly what paper-only requires.
        if resolved_base:
            raise PaperTradingEnforcementError(
                f"Custom Alpaca endpoint {resolved_base!r} is not supported by "
                "the installed SDK; refusing to guess."
            )
        client = TradingClient(api_key, api_secret, paper=True)
    # alpaca-py exposes no public timeout option and otherwise retries 429s
    # for every HTTP method. Execution owns retry semantics: disable the SDK
    # retry loop (especially for POST) and give requests fixed connect/read
    # timeouts so ambiguous mutation outcomes reach UNKNOWN promptly.
    session = getattr(client, "_session", None)
    request = getattr(session, "request", None)
    if session is None or not callable(request) or not hasattr(client, "_retry"):
        raise PaperTradingEnforcementError(
            "Installed alpaca-py cannot enforce fixed execution timeouts/retry policy"
        )
    client._retry = 0
    session.request = partial(request, timeout=(3.05, 10.0))
    return client


def _parse_timeframe(tf: Union[str, TimeFrame]) -> TimeFrame:
    """Convert a string like '5Min' or a TimeFrame instance into a TimeFrame."""
    if isinstance(tf, TimeFrame):
        return tf

    tf = tf.strip()
    low = tf.lower()
    
    # mapping common strings
    if low == "1min":
        result = TimeFrame.Minute
    elif low.endswith("min"):
        # e.g. "5Min", "15min"
        amount = int(tf[:-3])
        result = TimeFrame(amount, TimeFrameUnit.Minute)
    elif low == "1hour" or low == "1h":
        result = TimeFrame.Hour
    elif low.endswith("hour"):
        amount = int(tf[:-4])
        result = TimeFrame(amount, TimeFrameUnit.Hour)
    elif low.endswith("h") and low[:-1].isdigit():
        # shorthand: "4h", "2h", etc.
        amount = int(low[:-1])
        result = TimeFrame(amount, TimeFrameUnit.Hour)
    elif low == "1day" or low == "1d":
        result = TimeFrame.Day
    elif low.endswith("day"):
        amount = int(tf[:-3])
        result = TimeFrame(amount, TimeFrameUnit.Day)
    elif low.endswith("d") and low[:-1].isdigit():
        # shorthand: "2d", "3d", etc.
        amount = int(low[:-1])
        result = TimeFrame(amount, TimeFrameUnit.Day)
    else:
        # fallback
        result = TimeFrame.Day
    
    return result


def _is_supported_data_fallback_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "subscription",
            "permission",
            "unauthorized",
            # nginx-level 401s arrive as HTML pages with this phrasing
            "authorization required",
            "forbidden",
            "not found",
            "empty",
            "rate limit",
            "too many requests",
            "timeout",
            "api key",
            "secret not found",
        )
    )


def _yfinance_fallback_data(
    symbol: str,
    start: pd.Timestamp,
    end: Optional[pd.Timestamp],
    timeframe: Union[str, TimeFrame],
) -> pd.DataFrame:
    config = get_config()
    if not config.get("data_fallback_enabled", False):
        return pd.DataFrame()

    tf_text = str(timeframe).lower()
    interval = "1d"
    if "hour" in tf_text or tf_text in ("1h",):
        interval = "1h"
    elif "min" in tf_text:
        return pd.DataFrame()

    try:
        import yfinance as yf

        yahoo_symbol = TickerUtils.convert_for_api(symbol, "yahoo")
        data = yf.download(
            yahoo_symbol,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d") if end is not None else None,
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as exc:
        print(f"YFinance fallback failed for {symbol}: {exc}")
        return pd.DataFrame()

    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [col[0] for col in data.columns]
    rename_map = {
        "Date": "timestamp",
        "Datetime": "timestamp",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    }
    df = data.reset_index().rename(columns=rename_map)
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [col for col in required if col not in df.columns]
    if missing:
        print(f"YFinance fallback returned malformed data for {symbol}: missing {missing}")
        return pd.DataFrame()
    return df[required].dropna().reset_index(drop=True)


class AlpacaUtils:
    @staticmethod
    def _get_searchable_assets(cache_seconds: int = 900) -> List[Dict[str, Any]]:
        """Return active equity and crypto assets, cached to keep symbol search responsive."""
        now = time.time()
        cached_assets = _ASSET_SEARCH_CACHE.get("assets") or []
        if cached_assets and now < _ASSET_SEARCH_CACHE.get("expires_at", 0):
            return cached_assets

        try:
            client = get_alpaca_trading_client()
            assets = []
            for asset_class in (AssetClass.US_EQUITY, AssetClass.CRYPTO):
                request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=asset_class)
                assets.extend(client.get_all_assets(request))

            searchable_assets = []
            seen_symbols = set()
            for asset in assets:
                result = _asset_to_search_result(asset)
                symbol = result["symbol"]
                if not symbol or symbol in seen_symbols:
                    continue
                seen_symbols.add(symbol)
                searchable_assets.append(result)

            _ASSET_SEARCH_CACHE["assets"] = searchable_assets
            _ASSET_SEARCH_CACHE["expires_at"] = now + cache_seconds
            return searchable_assets
        except Exception as e:
            print(f"Error loading Alpaca assets for search: {e}")
            fallback = _fallback_asset_results()
            _ASSET_SEARCH_CACHE["assets"] = fallback
            _ASSET_SEARCH_CACHE["expires_at"] = now + 60
            return fallback

    @staticmethod
    def search_assets(query: str = "", limit: int = 12) -> List[Dict[str, Any]]:
        """Search active Alpaca equity and crypto assets for the WebUI symbol picker."""
        query = (query or "").strip().upper()
        normalized_query = query.replace("/", "").replace("-", "")
        assets = AlpacaUtils._get_searchable_assets()

        if not normalized_query:
            default_symbols = ["NVDA", "AMD", "TSLA", "AAPL", "MSFT", "BTC/USD", "ETH/USD", "SOL/USD"]
            by_symbol = {asset["symbol"]: asset for asset in assets}
            return [by_symbol.get(symbol) or asset for symbol in default_symbols for asset in _fallback_asset_results() if asset["symbol"] == symbol][:limit]

        matches = []
        for asset in assets:
            symbol = asset["symbol"]
            symbol_key = symbol.replace("/", "")
            crypto_base = symbol.split("/", 1)[0] if asset.get("asset_class") == AssetClass.CRYPTO.value else ""
            crypto_quote = symbol.split("/", 1)[1] if "/" in symbol else ""
            name = (asset.get("name") or "").upper()
            if crypto_base and normalized_query == crypto_base:
                quote_priority = {"USD": 0, "USDC": 0.1, "USDT": 0.2}.get(crypto_quote, 0.4)
                score = -1 + quote_priority
            elif normalized_query == symbol_key:
                score = 0
            elif symbol_key.startswith(normalized_query):
                score = 1
            elif name.startswith(query):
                score = 2
            elif normalized_query in symbol_key or query in name:
                score = 3
            else:
                continue

            tradable_penalty = 0 if asset.get("tradable") else 1
            crypto_bonus = 0 if asset.get("asset_class") != AssetClass.CRYPTO.value else -0.1
            matches.append((score + tradable_penalty + crypto_bonus, symbol, asset))

        matches.sort(key=lambda item: (item[0], item[1]))
        return [asset for _, _, asset in matches[:limit]]

    @staticmethod
    def get_stock_data(
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Optional[Union[str, datetime]] = None,
        timeframe: Union[str, TimeFrame] = "1Day",
        save_path: Optional[str] = None,
        feed: DataFeed = DataFeed.IEX
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV data for a stock or crypto symbol.

        Args:
            symbol: The ticker symbol (e.g. "SPY" or "BTC/USD")
            start_date: 'YYYY-MM-DD' string or datetime
            end_date: optional 'YYYY-MM-DD' string or datetime
            timeframe: e.g. "1Min","5Min","15Min","1Hour","1Day" or a TimeFrame instance
            save_path: if provided, path to write a CSV
            feed: DataFeed enum (default IEX)

        Returns:
            pandas DataFrame with columns ['timestamp','open','high','low','close','volume']
        """
        # normalize dates
        start = pd.to_datetime(start_date)
        end = pd.to_datetime(end_date) + timedelta(days=1) if end_date else None

        tf = _parse_timeframe(timeframe)

        try:
            # choose client
            is_crypto = "/" in symbol
            client = get_alpaca_crypto_client() if is_crypto else get_alpaca_stock_client()

            # build request params; always use a list for symbol_or_symbols
            params = (
                CryptoBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=tf,
                    start=start,
                    end=end,
                    feed=feed
                ) if is_crypto else
                StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=tf,
                    start=start,
                    end=end,
                    feed=feed
                )
            )
            bars = client.get_crypto_bars(params) if is_crypto else client.get_stock_bars(params)
            # convert to DataFrame via the .df property
            df = bars.df.reset_index()  # multi-index ['symbol','timestamp']
            
            # filter for our symbol (in case of list) - only if symbol column exists
            if "symbol" in df.columns:
                df = df[df["symbol"] == symbol].drop(columns="symbol")
            else:
                # If no symbol column, assume all data is for the requested symbol
                pass
                
            if df.empty:
                raise ValueError("empty Alpaca bar response")

            if save_path:
                df.to_csv(save_path, index=False, encoding="utf-8")
            return df

        except Exception as e:
            if _is_supported_data_fallback_error(e):
                fallback_df = _yfinance_fallback_data(symbol, start, end, timeframe)
                if not fallback_df.empty:
                    if save_path:
                        fallback_df.to_csv(save_path, index=False, encoding="utf-8")
                    return fallback_df
            print(f"Error fetching data for {symbol}: {e}")
            return pd.DataFrame()

    @staticmethod
    def get_latest_quote(symbol: str) -> dict:
        """
        Get the latest bid/ask quote for a symbol.
        """
        is_crypto = "/" in symbol
        client = get_alpaca_crypto_client() if is_crypto else get_alpaca_stock_client()
        req = CryptoLatestQuoteRequest(symbol_or_symbols=[symbol]) if is_crypto else StockLatestQuoteRequest(symbol_or_symbols=[symbol])
        try:
            resp = client.get_crypto_latest_quote(req) if is_crypto else client.get_stock_latest_quote(req)
            quote = resp[symbol]
            return {
                "symbol": symbol,
                "bid_price": quote.bid_price,
                "bid_size": quote.bid_size,
                "ask_price": quote.ask_price,
                "ask_size": quote.ask_size,
                "timestamp": quote.timestamp
            }
        except Exception as e:
            print(f"Error fetching latest quote for {symbol}: {e}")
            return {}

    
    @staticmethod
    def get_stock_data_window(
        symbol: Annotated[str, "ticker symbol"],
        curr_date: Annotated[str, "Current date in yyyy-mm-dd format"] = None,
        look_back_days: Annotated[int, "Number of days to look back"] = 30,
        timeframe: Annotated[str, "Timeframe for data: 1Min, 5Min, 15Min, 1Hour, 1Day"] = "1Day",
    ) -> pd.DataFrame:
        """
        Fetches historical stock data from Alpaca for the specified symbol and a window of days.
        
        Args:
            symbol: The stock ticker symbol
            curr_date: Current date in yyyy-mm-dd format (optional - if not provided, will use today's date)
            look_back_days: Number of days to look back
            timeframe: Timeframe for data (1Min, 5Min, 15Min, 1Hour, 1Day)
            
        Returns:
            DataFrame containing the historical stock data
        """
        # Calculate start date based on look_back_days
        if curr_date:
            curr_dt = pd.to_datetime(curr_date)
        else:
            curr_dt = pd.to_datetime(datetime.now().strftime("%Y-%m-%d"))
            
        start_dt = curr_dt - pd.Timedelta(days=look_back_days)
        
        # Don't pass end_date to avoid subscription limitations
        return AlpacaUtils.get_stock_data(
            symbol=symbol,
            start_date=start_dt.strftime("%Y-%m-%d"),
            timeframe=timeframe
        ) 

    @staticmethod
    def get_company_name(symbol: str) -> str:
        """
        Get company name for a ticker symbol using Alpaca API.
        
        Args:
            symbol: The ticker symbol (e.g. "AAPL")
            
        Returns:
            Company name as string or original symbol if not found
        """
        try:
            # Skip crypto or symbols with special characters
            if "/" in symbol:
                return symbol
                
            client = get_alpaca_trading_client()
            asset = client.get_asset(symbol)
            
            if asset and hasattr(asset, 'name') and asset.name:
                return asset.name
            else:
                # Use fallback if name is not available
                print(f"No company name found for {symbol} via API, using fallback.")
                return ticker_to_company_fallback.get(symbol, symbol)
                
        except Exception as e:
            print(f"Error fetching company name for {symbol}: {e}")
            print("This might be due to invalid API keys or insufficient permissions.")
            print("If you recently reset your paper trading account, you may need to generate new API keys.")
            return ticker_to_company_fallback.get(symbol, symbol) 

    @staticmethod
    def get_positions_data():
        """Get current positions from Alpaca account.

        N14: a broker/API/parse failure raises (with the exception type and
        message only — never secrets or headers) so the WebUI renders its
        error state. Only a real, successful response may produce output: a
        legal empty account returns [] and is displayed as empty.
        """
        try:
            client = get_alpaca_trading_client()
            positions = client.get_all_positions()
        except Exception as e:
            raise AlpacaAccountDataError("positions", e) from e

        # Convert positions to a list of dictionaries
        positions_data = []
        for position in positions:
            try:
                current_price = float(position.current_price)
                avg_entry_price = float(position.avg_entry_price)
                qty = float(position.qty)
                market_value = float(position.market_value)
                cost_basis = avg_entry_price * qty

                # Calculate P/L values
                today_pl_dollars = float(position.unrealized_intraday_pl)
                total_pl_dollars = float(position.unrealized_pl)
                today_pl_percent = (today_pl_dollars / cost_basis) * 100 if cost_basis != 0 else 0
                total_pl_percent = (total_pl_dollars / cost_basis) * 100 if cost_basis != 0 else 0
            except (TypeError, ValueError, AttributeError) as e:
                symbol = getattr(position, "symbol", "?")
                raise AlpacaAccountDataError(
                    f"position payload for {symbol}", e
                ) from e

            positions_data.append({
                "Symbol": position.symbol,
                "Qty": qty,
                "Market Value": f"${market_value:.2f}",
                "Avg Entry": f"${avg_entry_price:.2f}",
                "Cost Basis": f"${cost_basis:.2f}",
                "Today's P/L (%)": f"{today_pl_percent:.2f}%",
                "Today's P/L ($)": f"${today_pl_dollars:.2f}",
                "Total P/L (%)": f"{total_pl_percent:.2f}%",
                "Total P/L ($)": f"${total_pl_dollars:.2f}"
            })

        return positions_data

    @staticmethod
    def get_recent_orders(page=1, page_size=7):
        """Get recent orders from Alpaca account, with simple pagination."""
        return AlpacaUtils.get_recent_orders_page(page=page, page_size=page_size).get("orders", [])

    @staticmethod
    def get_recent_orders_page(page=1, page_size=5, max_orders=500):
        """Get recent Alpaca orders and pagination metadata for the WebUI.

        N14: a broker/API failure raises so the WebUI shows its error state;
        a successful response with no orders is a real, different fact.
        """
        try:
            client = get_alpaca_trading_client()
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                limit=max_orders,
                direction=Sort.DESC,
                nested=False,
            )
            orders_page = client.get_orders(req)
            orders = list(orders_page)

            orders_data = []
            for order in orders:
                qty = float(order.qty) if order.qty is not None else 0.0
                filled_qty = float(order.filled_qty) if order.filled_qty is not None else 0.0
                filled_avg_price = float(order.filled_avg_price) if order.filled_avg_price is not None else 0.0

                orders_data.append({
                    "Asset": order.symbol,
                    "Order Type": order.type,
                    "Side": order.side,
                    "Qty": qty,
                    "Filled Qty": filled_qty,
                    "Avg. Fill Price": f"${filled_avg_price:.2f}" if filled_avg_price > 0 else "-",
                    "Status": order.status,
                    "Source": order.client_order_id
                })

            total_orders = len(orders_data)
            total_pages = max(1, (total_orders + page_size - 1) // page_size)
            page = max(1, min(int(page or 1), total_pages))
            start = (page - 1) * page_size
            return {
                "orders": orders_data[start : start + page_size],
                "page": page,
                "page_size": page_size,
                "total_orders": total_orders,
                "total_pages": total_pages,
                "has_more": total_orders >= max_orders,
            }

        except Exception as e:
            raise AlpacaAccountDataError("orders", e) from e

    @staticmethod
    def get_account_info():
        """Get account information from Alpaca.

        N14: a broker/API/parse failure raises instead of returning a zero
        dict — an outage must never be displayed as $0 buying power/cash.
        A real account that legitimately reports 0 still renders 0.
        """
        try:
            client = get_alpaca_trading_client()
            account = client.get_account()

            # Extract the required values
            buying_power = float(account.buying_power)
            cash = float(account.cash)

            # Calculate daily change
            equity = float(account.equity)
            last_equity = float(account.last_equity)
        except Exception as e:
            raise AlpacaAccountDataError("account info", e) from e
        daily_change_dollars = equity - last_equity
        daily_change_percent = (daily_change_dollars / last_equity) * 100 if last_equity != 0 else 0

        return {
            "buying_power": buying_power,
            "cash": cash,
            "daily_change_dollars": daily_change_dollars,
            "daily_change_percent": daily_change_percent
        }

    @staticmethod
    def get_current_position_state(symbol: str, strict: bool = False) -> str:
        """Return current position state for a symbol in the Alpaca account.

        Args:
            symbol: Ticker symbol (e.g. "AAPL" or "BTC/USD").  Crypto symbols will
                    be treated the same way as equities – a positive quantity is
                    considered a *LONG* position while a negative quantity (should
                    Alpaca ever allow it) is considered *SHORT*.
            strict: When True, re-raise broker/API errors instead of defaulting
                    to "NEUTRAL".  The NEUTRAL fallback exists so agent prompts
                    keep working through an outage; order execution paths must
                    pass strict=True, because acting on a guessed NEUTRAL can
                    re-buy an existing position or silently skip an exit.

        Returns:
            One of "LONG", "SHORT", or "NEUTRAL" if no open position exists (or,
            when strict is False, when we encounter an error).
        """
        try:
            # Skip if credentials are missing – the helper will raise inside but we
            # want to fail gracefully and just assume no position.
            client = get_alpaca_trading_client()

            # `get_all_positions()` is more broadly supported across Alpaca
            # versions than `get_position(symbol)` and avoids raising when the
            # asset is not found.
            positions = client.get_all_positions()

            # Normalise the requested symbol for comparisons – Alpaca symbols
            # for crypto may use different formats, so we normalize for position comparison only.
            requested_symbol_key = symbol.upper().replace("/", "")

            for pos in positions:
                if pos.symbol.upper() == requested_symbol_key:
                    try:
                        qty = float(pos.qty)
                    except (ValueError, AttributeError):
                        if strict:
                            # A corrupted qty on the *target* position is as
                            # unsafe as an outage for an execution caller:
                            # guessing NEUTRAL here re-buys a real holding or
                            # skips a real exit. Let it propagate.
                            raise
                        qty = 0.0

                    if not math.isfinite(qty):
                        # e.g. qty "nan"/"inf" parses without raising but is
                        # not a real position size. Same fail-open risk as a
                        # malformed string for an execution caller.
                        if strict:
                            raise ValueError(
                                f"non-finite position qty for {symbol}: {pos.qty!r}"
                            )
                        return "NEUTRAL"

                    if qty > 0:
                        return "LONG"
                    elif qty < 0:
                        return "SHORT"
                    else:
                        # Zero quantity technically shouldn't appear but treat as
                        # neutral just in case.
                        return "NEUTRAL"
            # If we fall through the loop there is no open position for symbol.
            return "NEUTRAL"
        except Exception as e:
            if strict:
                # Execution callers must not mistake an outage for "no
                # position": a wrong NEUTRAL turns a BUY into pyramiding an
                # existing holding and a SELL into a skipped exit.
                raise
            # Log and default to neutral so agent prompts still work.
            print(f"Error determining current position for {symbol}: {e}")
            return "NEUTRAL"

    # NOTE (Phase A.1 remediation): direct broker-mutation helpers
    # (place_market_order / place_protected_market_order / close_position)
    # were removed. The single production order path is
    # tradingagents.execution.ExecutionService, which owns the durable
    # outbox, deterministic client_order_id, state machine and UNKNOWN
    # lookup/adopt. AlpacaUtils keeps data/query helpers only.

    @staticmethod
    def get_account_risk_snapshot() -> dict:
        """Numeric account snapshot for the deterministic risk engine.

        Unlike get_account_info, this raises on API failure instead of
        returning zeros: silent zero equity would read as "no capital" and
        corrupt every downstream sizing decision.
        """
        client = get_alpaca_trading_client()
        account = client.get_account()
        equity = float(account.equity)
        gross_exposure = 0.0
        for position in client.get_all_positions():
            try:
                gross_exposure += abs(float(position.market_value))
            except (TypeError, ValueError):
                continue
        return {"equity": equity, "gross_exposure": gross_exposure}

    @staticmethod
    def compute_risk_sized_amount(
        symbol: str,
        confidence: str,
        requested_notional: float,
        risk_params: Optional[dict] = None,
        side: str = "buy",
        authoritative_snapshot: Optional[Any] = None,
        authoritative_quote: Optional[Any] = None,
        stop_loss_price: Optional[float] = None,
    ) -> SizingDecision:
        """Run the deterministic sizing engine against live account/market data.

        Raises when required data (account snapshot, price) is unavailable so
        the caller can decide whether to fail open or block.
        """
        params = RiskParameters.from_dict(risk_params)
        if authoritative_snapshot is not None:
            snapshot = {
                "equity": authoritative_snapshot.equity,
                "gross_exposure": authoritative_snapshot.gross_exposure,
            }
        else:
            snapshot = AlpacaUtils.get_account_risk_snapshot()

        if authoritative_quote is not None:
            price = authoritative_quote.price
        else:
            quote = AlpacaUtils.get_latest_quote(symbol)
            quoted = [
                float(p)
                for p in (quote.get("bid_price"), quote.get("ask_price"))
                if p and float(p) > 0
            ]
            price = sum(quoted) / len(quoted) if quoted else None

        bars = AlpacaUtils.get_stock_data_window(
            symbol, look_back_days=max(40, params.atr_period * 3)
        )
        atr = compute_atr(bars, period=params.atr_period)

        if price is None:
            try:
                price = float(bars["close"].iloc[-1])
            except Exception:
                raise ValueError(f"No price data available for {symbol}")

        return PositionSizer(params).size_position(
            equity=snapshot["equity"],
            price=price,
            atr=atr,
            confidence=confidence,
            requested_notional=requested_notional,
            current_gross_exposure=snapshot["gross_exposure"],
            side=side,
            stop_loss_price=stop_loss_price,
        )

    @staticmethod
    def execute_trade_intent(
        symbol: str,
        current_position: str,
        trade_intent: Union["TradeIntent", Dict[str, Any]],
        dollar_amount: float,
        allow_shorts: bool = False,
        risk_params: Optional[dict] = None,
        db_path: Optional[str] = None,
        broker_factory: Optional[Any] = None,
        quote_factory: Optional[Any] = None,
    ) -> dict:
        """Validate and execute a typed TradeIntent via the single durable entry.

        Deprecated compatibility wrapper: this method performs no broker calls
        itself. All order/close POSTs go through
        tradingagents.execution.ExecutionService (durable outbox, two-layer
        idempotency, order state machine, UNKNOWN lookup/adopt, deterministic
        sizing and broker-side protective legs). Raw-signal execution
        (AlpacaUtils.execute_trading_action) is disabled.
        """
        # Imported lazily: a module-level import of the agents package from
        # here creates a dataflows <-> agents import cycle that breaks
        # whenever dataflows is imported first.
        from tradingagents.agents.schemas import TradeIntent

        try:
            intent = (
                trade_intent
                if isinstance(trade_intent, TradeIntent)
                else TradeIntent.model_validate(trade_intent)
            )
        except Exception as e:
            return {"success": False, "error": f"Invalid trade intent: {e}"}

        requested_symbol = (intent.symbol or "").upper().replace("/", "")
        actual_symbol = (symbol or "").upper().replace("/", "")
        if requested_symbol and requested_symbol != actual_symbol:
            return {
                "success": False,
                "error": f"Trade intent symbol {intent.symbol} does not match execution symbol {symbol}",
                "trade_intent": intent.model_dump(mode="json"),
            }

        from tradingagents.execution.service import ExecutionService

        svc = ExecutionService(
            db_path=db_path, broker_factory=broker_factory, quote_factory=quote_factory
        )
        return svc.execute(
            trade_intent=intent.model_dump(mode="json"),
            dollar_amount=dollar_amount,
            allow_shorts=allow_shorts,
            risk_params=risk_params,
            current_position=current_position,
        )

    @staticmethod
    def _safety_context(symbol: str):
        """Best-effort account snapshot for the safety layer's breakers.

        Returns (account, position_value); either may be None when the broker
        is unreachable — the guard reports those checks as skipped instead of
        guessing.
        """
        try:
            client = get_alpaca_trading_client()
            acct = client.get_account()
            account = {
                "equity": float(acct.equity),
                "last_equity": float(acct.last_equity),
            }
        except Exception:
            return None, None

        position_value = 0.0
        try:
            key = symbol.upper().replace("/", "")
            for pos in client.get_all_positions():
                if pos.symbol.upper() == key:
                    position_value = abs(float(pos.market_value))
                    break
        except Exception:
            position_value = None
        return account, position_value

    @staticmethod
    def execute_trading_action(symbol: str, current_position: str, signal: str,
                             dollar_amount: float, allow_shorts: bool = False,
                             protective_prices: Optional[Dict[str, float]] = None) -> dict:
        """Legacy signal execution: permanently disabled (fail-closed).

        Phase A.1 remediation: raw-signal to broker orchestration was a second
        execution engine bypassing the durable outbox. It now returns zero
        broker calls unconditionally. Use ExecutionService with a
        schema-valid TradeIntent.
        """
        _ = (symbol, current_position, signal, dollar_amount, allow_shorts, protective_prices)
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": (
                "Legacy signal execution is disabled in Phase A.1. "
                "Use ExecutionService with a schema-valid TradeIntent; "
                "no broker call was made."
            ),
        }
