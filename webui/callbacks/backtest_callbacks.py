"""
Backtest callbacks for TradingAgents WebUI
Runs the walk-forward backtest over recorded agent decisions and renders
metrics, the equity curve, and per-window robustness results.
"""

import dash_bootstrap_components as dbc
import plotly.graph_objects as go
from dash import Input, Output, State, html

from webui.config.constants import COLORS


def _fmt_pct(value):
    return "—" if value is None else f"{value:+.2%}"


def _fmt_ratio(value):
    return "—" if value is None else f"{value:.2f}"


def _metric_color(value, invert=False):
    if value is None:
        return COLORS["pending"]
    good = value < 0 if invert else value > 0
    return COLORS["completed"] if good else COLORS["error"]


def _metric_card(label, text, color):
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(label, className="text-muted small"),
                    html.Div(text, style={"color": color, "fontSize": "1.3rem", "fontWeight": 600}),
                ],
                className="p-2 text-center",
            ),
            style={"backgroundColor": COLORS["card"], "border": f"1px solid {COLORS['border']}"},
        ),
        md=2,
        xs=6,
        className="mb-2",
    )


def _build_metric_cards(metrics, signals_used):
    sharpe = metrics.get("sharpe_ratio")
    return dbc.Row(
        [
            _metric_card(
                "Cumulative Return",
                _fmt_pct(metrics.get("cumulative_return")),
                _metric_color(metrics.get("cumulative_return")),
            ),
            _metric_card(
                "Annualized Return",
                _fmt_pct(metrics.get("annualized_return")),
                _metric_color(metrics.get("annualized_return")),
            ),
            _metric_card("Sharpe Ratio", _fmt_ratio(sharpe), _metric_color(sharpe)),
            _metric_card(
                "Max Drawdown",
                "—" if metrics.get("max_drawdown") is None else f"-{metrics['max_drawdown']:.2%}",
                _metric_color(metrics.get("max_drawdown"), invert=True)
                if metrics.get("max_drawdown")
                else COLORS["pending"],
            ),
            _metric_card(
                "Win Rate",
                "—" if metrics.get("win_rate") is None else f"{metrics['win_rate']:.0%}",
                COLORS["primary"],
            ),
            _metric_card(
                "Trades / Signals",
                f"{metrics.get('num_trades', 0)} / {signals_used}",
                COLORS["text"],
            ),
        ],
        className="g-2",
    )


def _build_equity_figure(equity_curve, symbol):
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=list(equity_curve.index),
            y=list(equity_curve.values),
            mode="lines",
            name="Equity",
            line={"color": COLORS["primary"], "width": 2},
            fill="tozeroy",
            fillcolor="rgba(59, 130, 246, 0.08)",
        )
    )
    figure.update_layout(
        title=f"Equity Curve — {symbol}",
        template="plotly_dark",
        paper_bgcolor=COLORS["card"],
        plot_bgcolor=COLORS["card"],
        margin={"l": 40, "r": 20, "t": 40, "b": 30},
        yaxis={"title": "Portfolio value ($)", "tickformat": ",.0f"},
        showlegend=False,
    )
    return figure


def _build_windows_table(windows):
    if len(windows) < 2:
        return None
    header = html.Thead(
        html.Tr(
            [
                html.Th("Window"),
                html.Th("Bars"),
                html.Th("Return"),
                html.Th("Sharpe"),
                html.Th("Max DD"),
                html.Th("Win Rate"),
            ]
        )
    )
    rows = []
    for window in windows:
        metrics = window["metrics"]
        cum = metrics.get("cumulative_return")
        rows.append(
            html.Tr(
                [
                    html.Td(f"{window['start_date']} → {window['end_date']}"),
                    html.Td(window["bars"]),
                    html.Td(
                        _fmt_pct(cum),
                        style={"color": _metric_color(cum)},
                    ),
                    html.Td(_fmt_ratio(metrics.get("sharpe_ratio"))),
                    html.Td(
                        "—" if metrics.get("max_drawdown") is None else f"-{metrics['max_drawdown']:.2%}"
                    ),
                    html.Td(
                        "—" if metrics.get("win_rate") is None else f"{metrics['win_rate']:.0%}"
                    ),
                ]
            )
        )
    return html.Div(
        [
            html.H6("Segmented diagnostic windows", className="mt-2"),
            dbc.Table([header, html.Tbody(rows)], bordered=False, hover=True, size="sm", striped=True),
        ]
    )


def register_backtest_callbacks(app):
    """Register backtest callbacks with the Dash app"""

    @app.callback(
        [
            Output("backtest-status", "children"),
            Output("backtest-metrics", "children"),
            Output("backtest-equity-graph", "figure"),
            Output("backtest-graph-container", "style"),
            Output("backtest-windows-table", "children"),
        ],
        Input("backtest-run-btn", "n_clicks"),
        [
            State("backtest-symbol-input", "value"),
            State("backtest-start-date", "value"),
            State("backtest-end-date", "value"),
            State("backtest-window-bars", "value"),
            State("backtest-allow-shorts", "value"),
            State("backtest-slippage-model", "value"),
        ],
        prevent_initial_call=True,
    )
    def run_backtest_callback(n_clicks, symbol, start_date, end_date, window_bars, allow_shorts, slippage_model):
        hidden = {"display": "none"}
        empty = go.Figure()

        symbol = (symbol or "").strip().upper()
        if not symbol:
            alert = dbc.Alert("Enter a symbol to backtest.", color="warning", className="mb-0")
            return alert, None, empty, hidden, None

        try:
            from tradingagents.backtest import run_recorded_walk_forward

            result = run_recorded_walk_forward(
                symbol,
                start_date=start_date or None,
                end_date=end_date or None,
                window_bars=int(window_bars or 63),
                allow_shorts=bool(allow_shorts),
                slippage_model=slippage_model or "fixed",
            )
        except ValueError as e:
            alert = dbc.Alert(str(e), color="warning", className="mb-0")
            return alert, None, empty, hidden, None
        except Exception as e:
            alert = dbc.Alert(f"Backtest failed: {e}", color="danger", className="mb-0")
            return alert, None, empty, hidden, None

        full = result.full_period
        slippage = full.slippage or {}
        slippage_text = {
            "fixed": f"slippage: {slippage.get('bps', 0):g} bps",
            "volatility": "slippage: volatility-scaled",
            "none": "slippage: none",
        }.get(slippage.get("model"), "slippage: n/a")
        status_bits = [
            f"{full.start_date} → {full.end_date}",
            f"{full.signals_used} recorded signal(s)",
            "execution: next-bar open",
            slippage_text,
        ]
        if full.rejected_orders:
            status_bits.append(f"{len(full.rejected_orders)} order(s) rejected on gaps")
        status = html.Div(" · ".join(status_bits), className="text-muted small")

        metrics_cards = _build_metric_cards(full.metrics, full.signals_used)
        figure = _build_equity_figure(full.equity_curve, symbol)
        windows_table = _build_windows_table(result.windows)
        return status, metrics_cards, figure, {"display": "block"}, windows_table

    @app.callback(
        Output("backtest-teach-status", "children"),
        Input("backtest-teach-btn", "n_clicks"),
        [
            State("backtest-symbol-input", "value"),
            State("backtest-start-date", "value"),
            State("backtest-end-date", "value"),
        ],
        prevent_initial_call=True,
    )
    def teach_memory_callback(n_clicks, symbol, start_date, end_date):
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return dbc.Alert(
                "Enter a symbol to teach from.", color="warning", className="mb-0"
            )

        try:
            from tradingagents.backtest import (
                default_agent_memories,
                teach_memories_from_history,
            )

            summary = teach_memories_from_history(
                symbol,
                default_agent_memories(),
                start_date=start_date or None,
                end_date=end_date or None,
            )
        except ValueError as e:
            return dbc.Alert(str(e), color="warning", className="mb-0")
        except Exception as e:
            return dbc.Alert(f"Teaching failed: {e}", color="danger", className="mb-0")

        taught = summary["decisions_taught"]
        if not taught and summary["decisions_skipped_duplicate"]:
            text = (
                f"Nothing new to teach for {symbol}: "
                f"{summary['decisions_skipped_duplicate']} decision(s) already in memory."
            )
            return dbc.Alert(text, color="info", className="mb-0")
        if not taught:
            text = (
                f"No teachable decisions for {symbol} "
                f"({summary['outcomes_computed']} outcome(s) computed, "
                f"{summary['decisions_skipped_no_state']} without a recorded final state; "
                "teaching also requires OpenAI embeddings)."
            )
            return dbc.Alert(text, color="warning", className="mb-0")

        text = (
            f"Taught {taught} decision(s) for {symbol} — "
            f"{summary['lessons_written']} lesson(s) written to the agent memories "
            f"({summary['decisions_skipped_duplicate']} duplicate(s) skipped, "
            f"{summary['decisions_skipped_no_state']} without final state)."
        )
        return dbc.Alert(text, color="success", className="mb-0")
