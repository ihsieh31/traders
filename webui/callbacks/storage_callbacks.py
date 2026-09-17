"""Restore and persist controls with a one-time trigger and no cross-callback cycle."""
from dash import Input, Output, State, ctx, no_update
from webui.utils.storage import get_default_settings

# IDs use hyphens while persisted keys use underscores.
CONTROL_IDS = [
    "ticker-input", "analyst-market", "analyst-social", "analyst-news",
    "analyst-fundamentals", "analyst-macro", "research-depth", "allow-shorts",
    "loop-enabled", "loop-interval", "market-hour-enabled", "market-hours-input",
    "trade-after-analyze", "trade-dollar-amount", "llm-provider", "backend-url",
    "output-language", "checkpoint-enabled", "quick-llm", "deep-llm",
    "quick-llm-custom-model", "deep-llm-custom-model", "google-thinking-level",
    "anthropic-effort", "analysis-provider", "analysis-model", "analysis-backend-url",
    "decision-provider", "decision-model", "decision-backend-url",
    "auto-screening-enabled", "screening-provider", "screening-model", "screening-backend-url",
]
CONTROL_IDS += [f"{role}-llm-{param}" for role in ("quick", "deep") for param in (
    "reasoning-effort", "verbosity", "summary", "temperature", "top-p",
    "max-output-tokens", "store", "parallel-tool-calls")]


def _parse_symbols(value):
    values = value if isinstance(value, list) else (value or "").split(",")
    return [str(s).strip().upper() for s in values if str(s).strip()]


def restore_settings(stored):
    settings = {**get_default_settings(), **(stored or {})}
    return tuple(settings.get(component.replace("-", "_"), no_update)
                 for component in CONTROL_IDS)


def register_storage_callbacks(app):
    @app.callback(
        [Output(component, "value", allow_duplicate=True) for component in CONTROL_IDS],
        Input("settings-hydration-trigger", "n_intervals"), State("settings-store", "data"),
        prevent_initial_call=True,
    )
    def hydrate_settings(interval, stored):
        return restore_settings(stored)

    @app.callback(
        Output("settings-store", "data"),
        [Input(component, "value") for component in CONTROL_IDS],
        [State("settings-store", "data"), State("settings-hydration-trigger", "n_intervals")],
        prevent_initial_call=True,
    )
    def save_settings(*args):
        from dash.exceptions import PreventUpdate
        values, stored, hydrated = args[:-2], args[-2], args[-1]
        if not hydrated:
            raise PreventUpdate
        settings = dict(stored or get_default_settings())
        settings.update({component.replace("-", "_"): value
                         for component, value in zip(CONTROL_IDS, values)})
        settings["ticker_input"] = ", ".join(_parse_symbols(settings["ticker_input"]))
        return no_update if settings == stored else settings
