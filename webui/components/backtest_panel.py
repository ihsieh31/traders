"""
webui/components/backtest_panel.py - recorded signal diagnostic panel

Replays recorded single-symbol signals against historical prices (zero LLM
cost) and reports diagnostic metrics. This is not out-of-sample training or
a full portfolio backtest and does not prove profitability.
"""

import dash_bootstrap_components as dbc
from dash import dcc, html


def create_backtest_panel():
    """Create the backtest panel card for the web UI."""
    controls = dbc.Row(
        [
            dbc.Col(
                [
                    dbc.Label("Symbol", html_for="backtest-symbol-input", className="small"),
                    dbc.Input(
                        id="backtest-symbol-input",
                        type="text",
                        placeholder="e.g. AAPL or BTC/USD",
                        debounce=True,
                    ),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label("Start date (optional)", html_for="backtest-start-date", className="small"),
                    dbc.Input(id="backtest-start-date", type="date"),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label("End date (optional)", html_for="backtest-end-date", className="small"),
                    dbc.Input(id="backtest-end-date", type="date"),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label("Window (bars)", html_for="backtest-window-bars", className="small"),
                    dbc.Input(
                        id="backtest-window-bars",
                        type="number",
                        value=63,
                        min=2,
                        step=1,
                    ),
                ],
                md=1,
            ),
            dbc.Col(
                [
                    dbc.Label("Slippage", html_for="backtest-slippage-model", className="small"),
                    dbc.Select(
                        id="backtest-slippage-model",
                        options=[
                            {"label": "Fixed (5 bps)", "value": "fixed"},
                            {"label": "Volatility-scaled", "value": "volatility"},
                            {"label": "None (frictionless)", "value": "none"},
                        ],
                        value="fixed",
                    ),
                ],
                md=2,
            ),
            dbc.Col(
                [
                    dbc.Label(" ", className="small d-block"),
                    dbc.Switch(
                        id="backtest-allow-shorts",
                        label="Allow shorts",
                        value=False,
                    ),
                ],
                md=1,
            ),
            dbc.Col(
                [
                    dbc.Label(" ", className="small d-block"),
                    dbc.Button(
                        [html.I(className="fas fa-flask me-2"), "Run Backtest"],
                        id="backtest-run-btn",
                        color="primary",
                        className="w-100",
                    ),
                ],
                md=2,
            ),
        ],
        className="g-2 align-items-end mb-3",
    )

    teach_row = dbc.Row(
        [
            dbc.Col(
                dbc.Button(
                    [html.I(className="fas fa-graduation-cap me-2"), "Teach Memory"],
                    id="backtest-teach-btn",
                    color="secondary",
                    outline=True,
                    size="sm",
                ),
                width="auto",
            ),
            dbc.Col(
                html.Div(
                    "Injects one dated lesson per recorded decision (with its "
                    "fixed-horizon hypothetical position return) into the "
                    "persistent agent memories — idempotent, zero LLM cost.",
                    className="text-muted small",
                ),
            ),
        ],
        className="g-2 align-items-center mb-2",
    )

    return dbc.Card(
        dbc.CardBody(
            [
                html.H4("Recorded Signal Diagnostic", className="mb-1"),
                html.Div(
                    "Replays recorded single-symbol signals. This is not "
                    "out-of-sample training or a full portfolio backtest and "
                    "does not prove profitability. Screening, live sizing, "
                    "protective-order behavior and complete research/trading "
                    "costs are not replayed.",
                    className="text-muted small mb-3",
                ),
                controls,
                teach_row,
                dcc.Loading(
                    id="backtest-teach-loading",
                    type="default",
                    children=html.Div(id="backtest-teach-status", className="mb-2"),
                ),
                dcc.Loading(
                    id="backtest-loading",
                    type="default",
                    children=html.Div(
                        [
                            html.Div(id="backtest-status", className="mb-2"),
                            html.Div(id="backtest-metrics", className="mb-3"),
                            html.Div(
                                dcc.Graph(
                                    id="backtest-equity-graph",
                                    config={"displayModeBar": False, "responsive": True},
                                    style={"height": "320px", "width": "100%"},
                                ),
                                id="backtest-graph-container",
                                style={"display": "none"},
                            ),
                            html.Div(id="backtest-windows-table"),
                        ]
                    ),
                ),
            ]
        ),
        className="mb-4",
    )
