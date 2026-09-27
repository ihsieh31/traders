"""Collect multi-year daily bars. This is the project's data foundation.

READ research/plan.md SECTIONS 4.1-4.3 FIRST.

Data sources
------------
``--source alpaca-sip`` (default)
    Alpaca ``DataFeed.SIP``, ``Adjustment.SPLIT``. Preferred when entitled,
    because it is the same feed production uses. It is the only source this
    project recommends: its calendar gap rate is 1.14%.

    There is deliberately NO IEX fallback: IEX volume is roughly 2-3% of
    consolidated, so the production ``$20M ADV20`` threshold would silently
    become a ~$600K threshold and stop meaning what it means in production.

``--source yfinance``
    Consolidated OHLCV from Yahoo. No market-data entitlement required.
    **TESTED AND REJECTED as a primary feed** -- it drops ~22% of calendar
    sessions, which makes ``validate_and_clean_bars`` fail closed and zeroes
    most cross-sections. Kept only for entitlement-free environments.

    Both sources deliver split-adjusted values. Do NOT apply a split adjustment
    on top of either one; doing so produced a 19x phantom error on every date
    before a split. See research/process.md S-17.

Resumable: one compressed CSV per batch under ``data/bars/``. Re-running skips
batches that already exist unless ``--force`` is given.

    python -m research.src.collect_bars --start 2016-01-01 --end 2026-09-01
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from typing import Dict, List, Optional

from research.src import common
from research.src.common import log

#: Alpaca's multi-symbol daily-bars request ceiling. Mirrors the production
#: default (default_config.py ``screening_bars_batch_size``).
BATCH_SIZE = 100

#: yfinance has no hard documented ceiling, but very large single requests are
#: the main source of silent partial failures. 100 keeps failures diagnosable.
YF_CHUNK = 100

#: The widest window this study hands to ``validate_and_clean_bars`` is 61
#: bars, so a symbol needs at least that much history.
MIN_HISTORY_SESSIONS = 61


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2023-01-01", help="First date (inclusive).")
    parser.add_argument("--end", default=None, help="Last date. Default: today.")
    parser.add_argument(
        "--source",
        choices=("alpaca-sip", "yfinance"),
        default="alpaca-sip",
        help=(
            "Bars provider. Default alpaca-sip: same feed production uses. "
            "yfinance is kept only as a documented, TESTED-AND-REJECTED "
            "fallback -- it drops ~22%% of calendar sessions, which zeroes most "
            "cross-sections (plan.md 4.3)."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Symbols per request. Default {BATCH_SIZE}.",
    )
    parser.add_argument(
        "--max-symbols", type=int, default=0, help="Cap the universe (smoke test). 0 = all."
    )
    parser.add_argument(
        "--symbols-file",
        default=None,
        help=(
            "Read the universe from a newline-separated file instead of the "
            "Alpaca asset API. Use this if you have no Alpaca credentials at all."
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-fetch batches already cached."
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="Seconds to sleep between batches (politeness / rate limiting).",
    )
    return parser.parse_args()


# --------------------------------------------------------------------------
# Universe
# --------------------------------------------------------------------------


def universe_from_file(path: str) -> List[dict]:
    symbols = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            token = line.strip().upper()
            if token and not token.startswith("#"):
                symbols.append({"symbol": token})
    if not symbols:
        raise SystemExit(f"no symbols found in {path}")
    log(f"universe: {len(symbols)} symbols from {path}")
    return symbols


def universe_from_alpaca() -> List[dict]:
    """ACTIVE, tradable US_EQUITY from the Alpaca trading API.

    Listing assets is free and needs no market-data entitlement. The
    ``market_cap`` field it attaches is deliberately ignored: this project has
    no point-in-time market cap, so filtering on it would be look-ahead bias.
    See research/plan.md section 4.4 item 2.
    """
    from tradingagents.screening.universe import fetch_us_equity_universe

    log("fetching ACTIVE tradable US_EQUITY universe (Alpaca trading API)")
    try:
        universe = fetch_us_equity_universe()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"Alpaca universe fetch failed: {type(exc).__name__}: {exc}\n"
            "Use --symbols-file with a newline-separated ticker list instead."
        )
    symbols = sorted({e["symbol"] for e in universe if e.get("symbol")})
    log(f"universe: {len(symbols)} symbols")
    return [{"symbol": s} for s in symbols]


# --------------------------------------------------------------------------
# Adjustment policy
# --------------------------------------------------------------------------
#
# IMPORTANT, verified 2026-09-27: yfinance's ``Close`` and ``Volume`` are
# ALREADY split-adjusted, and they already match Alpaca's
# ``Adjustment.SPLIT`` exactly. Measured on AMZN, which had a 20:1 split on
# 2022-06-06:
#
#   2016-01-04   yfinance Close 31.8495   Alpaca SIP Close 31.8500
#   2022-06-03   yfinance Close 121.68    Alpaca SIP Close 121.68
#
# An earlier revision of this file applied a ``split_only_adjust`` helper on
# top of yfinance output. That double-adjusted the series and produced a
# spurious 19x discrepancy against SIP on every pre-split date. Do NOT
# reintroduce a split adjustment here. ``--source yfinance`` passes the Yahoo
# columns straight through, which is correct. verify_data.py compares the two
# feeds directly for the same reason.


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------


def fetch_batch_yfinance(symbols: List[str], start: date, end: date):
    """Consolidated daily OHLCV from Yahoo, split-adjusted like production."""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    raw = yf.download(
        list(symbols),
        start=start.isoformat(),
        # yfinance's end is exclusive; add a day so --end behaves inclusively.
        end=(datetime(end.year, end.month, end.day) + __import__("datetime").timedelta(days=1)).date().isoformat(),
        auto_adjust=False,
        actions=True,
        group_by="ticker",
        progress=False,
        threads=True,
        timeout=30,
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=list(common.BAR_COLUMNS) + ["symbol"])

    out = []
    for symbol in symbols:
        try:
            block = raw[symbol] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        if block is None or len(block) == 0:
            continue
        block = block.dropna(subset=["Close", "Volume"])
        if len(block) == 0:
            continue
        block = block.reset_index().rename(
            columns={
                "Date": "timestamp",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
                "Stock Splits": "splits",
            }
        )
        if "splits" not in block.columns:
            block["splits"] = 0.0
        block["splits"] = pd.to_numeric(block["splits"], errors="coerce").fillna(0.0)
        block = block[["timestamp", "open", "high", "low", "close", "volume", "splits"]]
        # No split adjustment here on purpose -- see the adjustment-policy
        # note above. Yahoo already delivers split-adjusted OHLCV.
        block["symbol"] = symbol
        out.append(block[["symbol", *common.BAR_COLUMNS]])

    if not out:
        return pd.DataFrame(columns=list(common.BAR_COLUMNS) + ["symbol"])
    frame = pd.concat(out, ignore_index=True)
    # Timestamps must be tz-aware: validate_and_clean_bars (metrics.py:206)
    # parses with utc=True and then converts to US/Eastern to get the session.
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame = frame[frame["timestamp"].notna()]
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close", "volume"])
    frame = frame[(frame["close"] > 0) & (frame["volume"] >= 0)]
    return frame.reset_index(drop=True)


def fetch_batch_alpaca_sip(symbols: List[str], start: date, end: date):
    """Alpaca consolidated SIP bars. Mirrors production parameters exactly."""
    import pandas as pd
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    from tradingagents.dataflows.alpaca_utils import (
        _apply_read_timeout,
        fetch_with_bounded_retry,
        get_alpaca_stock_client,
    )

    client = get_alpaca_stock_client()
    _apply_read_timeout(client, timeout=(3.05, 60.0))
    request = StockBarsRequest(
        # Field name matters: alpaca-py 0.44.0 renamed ``symbol_or`` to
        # ``symbol_or_symbols``. Production was already migrated
        # (screening/metrics.py:581); mirror it exactly.
        symbol_or_symbols=list(symbols),
        timeframe=TimeFrame.Day,
        start=datetime(start.year, start.month, start.day),
        end=datetime(end.year, end.month, end.day),
        adjustment=Adjustment.SPLIT,
        feed=DataFeed.SIP,
    )
    bars = fetch_with_bounded_retry(lambda: client.get_stock_bars(request), attempts=3)
    frame = getattr(bars, "df", None)
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=list(common.BAR_COLUMNS) + ["symbol"])
    frame = frame.reset_index()
    if "symbol" not in frame.columns:
        if frame.index.name == "symbol":
            frame = frame.reset_index()
        else:
            # The response shape is not one we recognise. Refuse rather than
            # writing rows with no identity: a cache entry with a null symbol
            # is silently invisible to the study and, if it reaches
            # prepare(), breaks the sort. Losing the batch is recoverable
            # (re-run); a poisoned cache is not.
            log(
                "  batch response carried no symbol column; refusing the batch "
                f"({len(frame)} rows) rather than writing unlabelled bars"
            )
            return pd.DataFrame(columns=list(common.BAR_COLUMNS) + ["symbol"])
    frame["symbol"] = frame["symbol"].astype(str)
    before = len(frame)
    frame = frame[frame["symbol"].str.strip() != ""]
    if len(frame) != before:
        log(f"  dropped {before - len(frame)} rows with an empty symbol")
    keep = [c for c in ("symbol", *common.BAR_COLUMNS) if c in frame.columns]
    return frame[keep].copy()


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()
    if end <= start:
        print("--end must be after --start", flush=True)
        return 2
    if args.batch_size < 1:
        print("--batch-size must be >= 1", flush=True)
        return 2

    if args.source == "yfinance":
        universe = (
            universe_from_file(args.symbols_file)
            if args.symbols_file
            else universe_from_alpaca()
        )
    else:
        common.require_credentials()
        universe = (
            universe_from_file(args.symbols_file)
            if args.symbols_file
            else universe_from_alpaca()
        )
    symbols = [e["symbol"] for e in universe]
    if args.max_symbols > 0 and len(symbols) > args.max_symbols:
        # Evenly spaced, NOT a prefix. The universe arrives sorted by symbol,
        # so taking the first N would yield only A-words and silently bias
        # every cross-section toward one alphabetical slice of the market.
        step = len(symbols) / args.max_symbols
        picked = [symbols[int(i * step)] for i in range(args.max_symbols)]
        symbols = sorted(set(picked))
        log(f"capped to {len(symbols)} symbols (evenly spaced, not a prefix)")

    chunk = YF_CHUNK if args.source == "yfinance" else args.batch_size
    batches = [symbols[i : i + chunk] for i in range(0, len(symbols), chunk)]
    log(
        f"plan: {len(batches)} batches of <= {chunk} over {start}..{end} "
        f"via {args.source}"
    )

    # One calendar fetch serves both this module and run_ic_study.py.
    sessions = common.calendar_sessions(common.load_calendar_rows(start, end))
    log(f"calendar proves {len(sessions)} sessions")

    fetch = fetch_batch_yfinance if args.source == "yfinance" else fetch_batch_alpaca_sip

    written = skipped = empty = failed = 0
    failed_symbols: List[str] = []
    for index, batch in enumerate(batches):
        path = common.batch_cache_path(index)
        if path.exists() and not args.force:
            skipped += 1
            continue
        label = f"{batch[0]}..{batch[-1]}"
        try:
            frame = fetch(batch, start, end)
        except Exception as exc:  # noqa: BLE001 - one bad batch must not kill the run
            failed += 1
            failed_symbols.extend(batch)
            log(f"batch {index:5d} [{label}] FAILED: {type(exc).__name__}: {exc}")
            continue

        if len(frame) == 0:
            empty += 1
            failed_symbols.extend(batch)
            log(f"batch {index:5d} [{label}] empty")
            continue

        counts = frame.groupby("symbol").size()
        short = counts[counts < MIN_HISTORY_SESSIONS].index.tolist()
        if short:
            frame = frame[~frame["symbol"].isin(short)]
        if len(frame) == 0:
            empty += 1
            failed_symbols.extend(batch)
            log(f"batch {index:5d} [{label}] all symbols under {MIN_HISTORY_SESSIONS} bars")
            continue

        rows = common.save_batch(index, frame)
        written += 1
        log(
            f"batch {index:5d} [{label}] -> {rows} rows, "
            f"{frame['symbol'].nunique()} symbols"
            f"{f', dropped {len(short)} short' if short else ''}"
        )
        if args.pause > 0:
            time.sleep(args.pause)

    log("=" * 72)
    log(f"batches: written={written} cached={skipped} empty={empty} failed={failed}")
    if failed_symbols:
        log(f"symbols with no usable history: {len(failed_symbols)}")
        if failed > 0 and args.source == "alpaca-sip":
            log(
                "  SIP failures are usually an entitlement problem. This study "
                "has no IEX fallback by design; use --source yfinance instead."
            )
    log(f"cache: {common.bars_coverage(common.load_all_bars())}")
    log(f"location: {common.BARS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
