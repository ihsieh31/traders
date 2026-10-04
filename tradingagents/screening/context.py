"""Carry frozen scan measurements into the final risk prompt, without rescanning."""

import json
import math
from datetime import date

from .metrics import FACTOR_KEYS
from .quality import QUALITY_VERSION


def screening_context_from_selection(selection):
    if not selection:
        return None
    pool_symbols = {f["symbol"] for f in selection.get("top40", [])}
    return {
        "as_of": selection.get("as_of"), "method": selection.get("method", "legacy"),
        "quality_version": (selection.get("data_quality") or {}).get("version"),
        "financial_quality": (selection.get("data_quality") or {}).get("financials", {}).get("status", "unknown"),
        "input_quality": {r["symbol"]: {k: r.get(k) for k in ("status", "reason", "identity_source", "asset_id", "bars")}
                          for r in (selection.get("data_quality") or {}).get("records", [])
                          if r.get("status") == "usable" and r.get("symbol") in pool_symbols},
        "rows": {row["symbol"]: {k: row.get(k) for k in ("price",) + FACTOR_KEYS}
                 for row in selection.get("top40", [])},
    }


def render_screening_context(config, symbol, trade_date):
    context = (config or {}).get("_screening_context")
    if not context:
        return ""
    unavailable = "Screening measurements unavailable for this held-review symbol; do not infer that it passed the gates."
    try:
        if date.fromisoformat(context["as_of"]) > date.fromisoformat(str(trade_date)):
            return unavailable
        row = context["rows"].get(symbol)
        if not row or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in row.values()):
            return unavailable
    except (KeyError, TypeError, ValueError):
        return unavailable
    return (
        f"Frozen screening measurements, as_of={context['as_of']}, method={context['method']}: "
        + json.dumps(row, sort_keys=True)
        + ". vol20 is annualized 20-session sample volatility; returns/trend are fractions, ADV20 is USD. "
        "Use these historical measurements to assess risk; screening priority is not a return forecast, "
        "probability, verified business quality, or permission to open/increase a position."
        + " Financial statement quality is unknown; missing financials are not zero or proof of a bad company."
        + (" Screening input quality evidence is unavailable; do not infer verified identity or prices."
           if context.get("quality_version") != QUALITY_VERSION or symbol not in context.get("input_quality", {})
           else " Identity and price checks apply to this frozen instrument snapshot, not historical issuer identity or business quality.")
    )
