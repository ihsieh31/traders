"""Walk-forward backtesting engine built on backtrader.

The engine replays a series of dated BUY/SELL/HOLD signals against
historical OHLCV bars. Orders are submitted when a bar closes and fill at
the *next* bar's open (backtrader's default, cheat-on-open disabled), so a
decision made on day t can never benefit from day t's own price — the same
information-set discipline required to avoid lookahead bias in walk-forward
evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import backtrader as bt
import pandas as pd

from .metrics import (
    CRYPTO_DAYS_PER_YEAR,
    TRADING_DAYS_PER_YEAR,
    summarize_performance,
)
from .signals import load_recorded_signals


_BPS = 1e-4  # one basis point as a fraction


class _VolatilitySlippageBroker(bt.brokers.BackBroker):
    """Backtesting broker whose slippage scales with recent volatility.

    Each fill slips by ``vol_fraction`` of the previous completed bar's range
    (high - low, as a fraction of its close), clamped to
    [``min_bps``, ``max_bps``] basis points.  Calm tape costs little; wide
    bars — where market orders realistically eat through the book — cost
    more.  The fill-bar's own range is never used, so the model needs no
    intrabar knowledge.
    """

    def __init__(self, vol_fraction=0.1, min_bps=1.0, max_bps=50.0):
        super().__init__()
        self._vol_fraction = float(vol_fraction)
        self._min_perc = float(min_bps) * _BPS
        self._max_perc = float(max_bps) * _BPS
        self._slippage_feed = None
        # Base-class slippage plumbing reads p.slip_perc; make fills at the
        # open slip like set_slippage_perc() would.
        self.p.slip_open = True
        self.p.slip_match = True
        self.p.slip_out = False

    def set_slippage_feed(self, data) -> None:
        self._slippage_feed = data

    def _dynamic_perc(self) -> float:
        feed = self._slippage_feed
        try:
            close = float(feed.close[-1])
            bar_range = max(float(feed.high[-1]) - float(feed.low[-1]), 0.0)
            if close <= 0:
                return self._min_perc
            perc = self._vol_fraction * bar_range / close
        except (IndexError, TypeError, AttributeError):
            # First bar (no completed predecessor) or no feed registered.
            return self._min_perc
        return min(max(perc, self._min_perc), self._max_perc)

    def _slip_up(self, pmax, price, doslip=True, lim=False):
        self.p.slip_perc = self._dynamic_perc()
        return super()._slip_up(pmax, price, doslip=doslip, lim=lim)

    def _slip_down(self, pmin, price, doslip=True, lim=False):
        self.p.slip_perc = self._dynamic_perc()
        return super()._slip_down(pmin, price, doslip=doslip, lim=lim)


class _SignalReplayStrategy(bt.Strategy):
    """Executes an externally supplied {date: action} map, nothing else."""

    params = (
        ("signals", None),  # dict[str iso-date, "BUY"|"SELL"|"HOLD"]
        ("allow_shorts", False),
        ("position_pct", 0.95),  # fraction of portfolio value per full position
    )

    def __init__(self):
        self.equity_dates: List[pd.Timestamp] = []
        self.equity_values: List[float] = []
        self.closed_trade_pnls: List[float] = []
        self.executed_orders: List[dict] = []
        self.rejected_orders: List[dict] = []
        # Signals sorted by date so ones falling on non-trading days (weekends,
        # halts) apply on the next available bar instead of silently dropping.
        self._pending = sorted((self.p.signals or {}).items())
        self._cursor = 0

    def _consume_signals_up_to(self, bar_date: str) -> Optional[str]:
        action = None
        while self._cursor < len(self._pending) and self._pending[self._cursor][0] <= bar_date:
            action = self._pending[self._cursor][1]
            self._cursor += 1
        return action

    def next(self):
        bar_date = self.data.datetime.date(0)
        self.equity_dates.append(pd.Timestamp(bar_date))
        self.equity_values.append(float(self.broker.getvalue()))

        action = self._consume_signals_up_to(bar_date.isoformat())
        if action in {"BUY", "LONG"}:
            self.order_target_percent(target=self.p.position_pct)
        elif action in {"SELL", "NEUTRAL"}:
            self.order_target_percent(target=0.0)
        elif action == "SHORT":
            if not self.p.allow_shorts:
                raise ValueError("SHORT signal requires allow_shorts=True")
            self.order_target_percent(target=-self.p.position_pct)
        # HOLD / None: keep the current position untouched.

    def notify_order(self, order):
        if order.status == order.Completed:
            self.executed_orders.append(
                {
                    "date": self.data.datetime.date(0).isoformat(),
                    "side": "buy" if order.isbuy() else "sell",
                    "size": float(order.executed.size),
                    "price": float(order.executed.price),
                }
            )
        elif order.status in (order.Margin, order.Rejected):
            # Sizing uses the signal bar's close but fills at the next open;
            # a large overnight gap can make the order unaffordable. Surface
            # that instead of letting the trade vanish silently.
            self.rejected_orders.append(
                {
                    "date": self.data.datetime.date(0).isoformat(),
                    "side": "buy" if order.isbuy() else "sell",
                    "status": "margin" if order.status == order.Margin else "rejected",
                }
            )

    def notify_trade(self, trade):
        if trade.isclosed:
            self.closed_trade_pnls.append(float(trade.pnlcomm))


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trade_pnls: List[float]
    orders: List[dict]
    metrics: dict
    signals_used: int
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    rejected_orders: List[dict] = field(default_factory=list)
    slippage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "evaluation_scope": "single_symbol_signal_diagnostic",
            "limitations": ["Not a replay of live position sizing, screening, protective orders or portfolio allocation",
                            "Returns exclude research/data costs, financing and dividends; not net strategy profitability"],
            "metrics": dict(self.metrics),
            "signals_used": self.signals_used,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "num_orders": len(self.orders),
            "rejected_orders": list(self.rejected_orders),
            "slippage": dict(self.slippage),
            "equity_curve": {
                ts.date().isoformat(): value
                for ts, value in self.equity_curve.items()
            },
        }


@dataclass
class WalkForwardResult:
    windows: List[dict] = field(default_factory=list)
    full_period: Optional[BacktestResult] = None

    def to_dict(self) -> dict:
        return {
            "evaluation_scope": "segmented_signal_diagnostic_not_out_of_sample_training",
            "windows": list(self.windows),
            "full_period": self.full_period.to_dict() if self.full_period else None,
        }


def normalize_price_frame(prices: pd.DataFrame) -> pd.DataFrame:
    """Coerce an OHLCV frame into the shape backtrader's PandasData expects.

    Accepts the ['timestamp', open, high, low, close, volume] layout produced
    by AlpacaUtils.get_stock_data (any column casing) or a frame already
    indexed by datetime. Raises ValueError on anything unusable.
    """
    if prices is None or len(prices) == 0:
        raise ValueError("No price data supplied for backtest.")

    frame = prices.copy()
    frame.columns = [str(c).lower() for c in frame.columns]

    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame = frame.set_index("timestamp")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("Price data needs a 'timestamp' column or a DatetimeIndex.")

    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)

    required = {"open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Price data is missing columns: {sorted(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0

    frame = frame[["open", "high", "low", "close", "volume"]].astype(float)
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame


def run_backtest(
    prices: pd.DataFrame,
    signals: Dict[str, str],
    initial_cash: float = 100_000.0,
    commission: float = 0.001,
    allow_shorts: bool = False,
    position_pct: float = 0.20,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    slippage_model: str = "fixed",
    slippage_bps: float = 5.0,
    slippage_vol_fraction: float = 0.1,
    slippage_min_bps: float = 1.0,
    slippage_max_bps: float = 50.0,
) -> BacktestResult:
    """Replay dated signals over price history and measure performance.

    Slippage models (zero-slippage backtests systematically overstate
    performance, so a small fixed cost is applied by default):

    - ``"fixed"``: every fill slips by ``slippage_bps`` basis points against
      the trade (default 5 bps).
    - ``"volatility"``: the slip is ``slippage_vol_fraction`` of the previous
      bar's high-low range (as a fraction of its close), clamped to
      [``slippage_min_bps``, ``slippage_max_bps``] — cheap in calm tape,
      expensive across wide bars.
    - ``"none"``: frictionless fills (not recommended outside tests).
    """
    frame = normalize_price_frame(prices)

    cerebro = bt.Cerebro()
    data_feed = bt.feeds.PandasData(dataname=frame)
    cerebro.adddata(data_feed)

    if slippage_model == "volatility":
        broker = _VolatilitySlippageBroker(
            vol_fraction=slippage_vol_fraction,
            min_bps=slippage_min_bps,
            max_bps=slippage_max_bps,
        )
        broker.set_slippage_feed(data_feed)
        cerebro.setbroker(broker)
        slippage_config = {
            "model": "volatility",
            "vol_fraction": float(slippage_vol_fraction),
            "min_bps": float(slippage_min_bps),
            "max_bps": float(slippage_max_bps),
        }
    elif slippage_model == "fixed":
        if float(slippage_bps) > 0:
            cerebro.broker.set_slippage_perc(float(slippage_bps) * _BPS)
        slippage_config = {"model": "fixed", "bps": float(slippage_bps)}
    elif slippage_model == "none":
        slippage_config = {"model": "none"}
    else:
        raise ValueError(
            f"Unknown slippage_model {slippage_model!r}; expected 'fixed', 'volatility', or 'none'."
        )

    cerebro.broker.setcash(float(initial_cash))
    cerebro.broker.setcommission(commission=float(commission))
    cerebro.addstrategy(
        _SignalReplayStrategy,
        signals=dict(signals or {}),
        allow_shorts=allow_shorts,
        position_pct=position_pct,
    )
    strategy = cerebro.run()[0]

    equity_curve = pd.Series(
        strategy.equity_values, index=strategy.equity_dates, dtype=float
    )
    metrics = summarize_performance(
        equity_curve,
        strategy.closed_trade_pnls,
        periods_per_year=periods_per_year,
    )
    return BacktestResult(
        equity_curve=equity_curve,
        trade_pnls=list(strategy.closed_trade_pnls),
        orders=list(strategy.executed_orders),
        metrics=metrics,
        signals_used=len(signals or {}),
        start_date=frame.index[0].date().isoformat(),
        end_date=frame.index[-1].date().isoformat(),
        rejected_orders=list(strategy.rejected_orders),
        slippage=slippage_config,
    )


def run_walk_forward(
    prices: pd.DataFrame,
    signals: Dict[str, str],
    window_bars: int = 63,
    min_window_bars: int = 5,
    **backtest_kwargs,
) -> WalkForwardResult:
    """Diagnose signals over disjoint windows; this is not an out-of-sample training protocol.

    LLM decision replay has no parameters to re-fit between folds, so the
    walk-forward's purpose here is robustness: instead of one full-period
    number, each ~quarterly window (63 trading bars by default) restarts with
    fresh capital and reports its own metrics, exposing regime dependence a
    single lucky backtest would hide. A trailing window shorter than
    `min_window_bars` is folded into its predecessor.
    """
    frame = normalize_price_frame(prices)
    if window_bars < 2:
        raise ValueError("window_bars must be at least 2.")

    boundaries = list(range(0, len(frame), window_bars))
    windows: List[dict] = []
    for start in boundaries:
        end = start + window_bars
        # Absorb a too-short trailing remainder into the final window.
        if len(frame) - end < min_window_bars:
            end = len(frame)
        chunk = frame.iloc[start:end]
        if len(chunk) < 2:
            break
        window_signals = {d: a for d, a in signals.items()
                          if chunk.index[0].date().isoformat() <= d <= chunk.index[-1].date().isoformat()}
        result = run_backtest(chunk, window_signals, **backtest_kwargs)
        windows.append(
            {
                "start_date": result.start_date,
                "end_date": result.end_date,
                "bars": len(chunk),
                "metrics": result.metrics,
            }
        )
        if end == len(frame):
            break

    full = run_backtest(frame, signals, **backtest_kwargs)
    return WalkForwardResult(windows=windows, full_period=full)


def run_recorded_backtest(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    eval_results_dir: str = "eval_results",
    price_loader: Optional[Callable[[str, str, Optional[str]], pd.DataFrame]] = None,
    **backtest_kwargs,
) -> BacktestResult:
    """Backtest the signals this deployment has already produced for `symbol`.

    Pulls decisions from the persisted run logs (zero LLM cost) and prices
    from Alpaca (with its existing yfinance fallback). `price_loader` exists
    for tests and alternative data sources.
    """
    signals = load_recorded_signals(symbol, eval_results_dir=eval_results_dir)
    if not signals:
        raise ValueError(
            f"No recorded completed runs with final signals found for {symbol} "
            f"under {eval_results_dir}/."
        )

    first_signal = min(signals)
    start = start_date or first_signal
    if start > first_signal:
        # Signals before the price window would misleadingly fire on its
        # first bar; drop them so the backtest matches the requested range.
        signals = {d: a for d, a in signals.items() if d >= start}
        if not signals:
            raise ValueError(
                f"All recorded signals for {symbol} predate start_date={start}."
            )

    if price_loader is None:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        price_loader = AlpacaUtils.get_stock_data

    prices = price_loader(symbol, start, end_date)
    backtest_kwargs.setdefault(
        "periods_per_year",
        CRYPTO_DAYS_PER_YEAR if "/" in symbol else TRADING_DAYS_PER_YEAR,
    )
    return run_backtest(prices, signals, **backtest_kwargs)


def run_recorded_walk_forward(
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    eval_results_dir: str = "eval_results",
    price_loader: Optional[Callable[[str, str, Optional[str]], pd.DataFrame]] = None,
    window_bars: int = 63,
    **backtest_kwargs,
) -> WalkForwardResult:
    """Walk-forward evaluation of this deployment's recorded decisions."""
    signals = load_recorded_signals(symbol, eval_results_dir=eval_results_dir)
    if not signals:
        raise ValueError(
            f"No recorded completed runs with final signals found for {symbol} "
            f"under {eval_results_dir}/."
        )

    start = start_date or min(signals)
    signals = {d: a for d, a in signals.items() if d >= start}
    if not signals:
        raise ValueError(
            f"All recorded signals for {symbol} predate start_date={start}."
        )

    if price_loader is None:
        from tradingagents.dataflows.alpaca_utils import AlpacaUtils

        price_loader = AlpacaUtils.get_stock_data

    prices = price_loader(symbol, start, end_date)
    backtest_kwargs.setdefault(
        "periods_per_year",
        CRYPTO_DAYS_PER_YEAR if "/" in symbol else TRADING_DAYS_PER_YEAR,
    )
    return run_walk_forward(prices, signals, window_bars=window_bars, **backtest_kwargs)
