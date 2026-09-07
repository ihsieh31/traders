# defillama_utils.py

import requests
import datetime
from typing import List, Dict, Tuple, Optional

from .interface_utils import HISTORICAL_SOURCE_UNAVAILABLE, analysis_date_mode

"""defillama_utils.py — lightweight helpers that pull free on‑chain fundamentals
from DeFi Llama’s open API so your agent can issue Buy / Sell / Hold
signals without paid data feeds.

Public API docs: https://defillama.com/docs/api
All endpoints used here require **no API key**.
"""

BASE_URL = "https://api.llama.fi"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_json(endpoint: str) -> Dict:
    """GET a DeFi Llama endpoint and return its JSON body.
    Raises requests.HTTPError on 4xx / 5xx.
    """
    resp = requests.get(f"{BASE_URL}{endpoint}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get_protocols() -> List[Dict]:
    """Return the full list of tracked protocols (cached by caller)."""
    return _fetch_json("/protocols")


def _find_slug(symbol: str) -> Tuple[Optional[str], Optional[str]]:
    """Map a **token symbol** to DeFi Llama *slug* and *name*.

    If the symbol represents a *base chain* (e.g. SOL, ETH) there may be no
    slug; in that case the caller should treat the symbol as a chain name and
    use chain‑level endpoints.
    """
    symbol = symbol.upper()
    for proto in _get_protocols():
        proto_symbol = proto.get("symbol")
        if not proto_symbol:
            continue
        # symbol can be string or list in API response
        symbols = (
            [s.upper() for s in proto_symbol]
            if isinstance(proto_symbol, list)
            else [proto_symbol.upper()]
        )
        if symbol in symbols:
            return proto.get("slug"), proto.get("name")
    return None, None


def _get_chain_fundamentals(symbol: str, lookback_days: int = 30) -> str:
    """Get fundamentals for a base chain (like ETH, SOL, etc.) using chain-level TVL data."""
    symbol = symbol.upper()
    
    # Map common symbols to their DeFi Llama chain names
    chain_mapping = {
        "ETH": "Ethereum",
        "SOL": "Solana", 
        "AVAX": "Avalanche",
        "MATIC": "Polygon",
        "BNB": "BSC",
        "FTM": "Fantom",
        "ATOM": "Cosmos",
        "ONE": "Harmony",
        "LUNA": "Terra",
        "DOT": "Polkadot"
    }
    
    chain_name = chain_mapping.get(symbol)
    if not chain_name:
        return f"Chain '{symbol}' not recognized. Supported chains: {', '.join(chain_mapping.keys())}"
    
    try:
        # Get current TVL for the chain
        current_tvl_json = _fetch_json(f"/v2/historicalChainTvl/{chain_name}")
        if not current_tvl_json:
            return f"No TVL data available for {chain_name}."
            
        # Get historical data points
        tvl_data = current_tvl_json
        if not tvl_data:
            return f"No historical TVL data available for {chain_name}."
            
        # Sort by timestamp and get latest
        sorted_data = sorted(tvl_data, key=lambda x: x['date'])
        latest_entry = sorted_data[-1]
        latest_tvl = latest_entry.get('tvl', 0)
        latest_ts = latest_entry['date']
        
        # Calculate TVL change over lookback period
        cutoff_ts = latest_ts - lookback_days * 86_400
        past_points = [p for p in sorted_data if p['date'] <= cutoff_ts]
        past_tvl = past_points[-1]['tvl'] if past_points else None
        tvl_pct = ((latest_tvl - past_tvl) / past_tvl * 100) if past_tvl else None
        
        latest_date = datetime.datetime.utcfromtimestamp(latest_ts).strftime("%Y-%m-%d")
        
        # Assemble markdown report
        lines = [
            f"### {chain_name} Ecosystem Fundamentals (as of {latest_date})\n",
            f"- **Total TVL:** ${latest_tvl:,.0f}",
        ]
        
        if tvl_pct is not None:
            lines.append(f"- **TVL Δ {lookback_days}d:** {tvl_pct:+.2f}%")
            
        # Try to get additional protocol count
        try:
            protocols_json = _fetch_json(f"/protocols")
            chain_protocols = [p for p in protocols_json if chain_name.lower() in [c.lower() for c in p.get('chains', [])]]
            if chain_protocols:
                lines.append(f"- **Active Protocols:** {len(chain_protocols)}")
        except Exception:
            pass  # Protocol count is nice-to-have
            
        return "\n".join(lines)
        
    except Exception as exc:
        return f"Error fetching chain fundamentals for {chain_name}: {exc}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_fundamentals(symbol: str, lookback_days: int = 30, curr_date: Optional[str] = None) -> str:
    """Return a markdown summary of free fundamentals for *symbol*.

    The summary includes:
      • Latest TVL (total value locked)
      • % change in TVL over *lookback_days*
      • Sum of protocol FEES and REVENUE over *lookback_days*

    Args:
        symbol: Token ticker such as 'UNI', 'SOL', 'GMX'.
        lookback_days: Window size for change / sum calculations.
        curr_date: Analysis as-of date (yyyy-mm-dd). Missing curr_date is
            only allowed for direct live calls; historical callers must pass
            the analysis date so the live-only source is rejected before HTTP.

    Returns:
        Markdown‑formatted string suitable for LLM prompts or dashboards.
    """
    # Live-only source: reject historical as-of before any HTTP call.
    if curr_date is not None and analysis_date_mode(curr_date) == "historical":
        return HISTORICAL_SOURCE_UNAVAILABLE

    # Check if this is a major base chain first (prioritize chain-level data)
    base_chains = ["ETH", "SOL", "AVAX", "MATIC", "BNB", "FTM", "ATOM", "ONE", "LUNA", "DOT"]
    if symbol.upper() in base_chains:
        return _get_chain_fundamentals(symbol, lookback_days)

    slug, nice_name = _find_slug(symbol)
    if not slug:
        # Fallback to chain fundamentals for other potential chains
        return _get_chain_fundamentals(symbol, lookback_days)

    # ---------------- TVL ----------------
    try:
        proto_json = _fetch_json(f"/protocol/{slug}")
        tvl_series = sorted(proto_json.get("tvl", []), key=lambda d: d["date"])
    except Exception as exc:
        return f"Error fetching TVL data: {exc}"

    if not tvl_series:
        return f"No TVL data available for '{symbol}'."

    latest_entry = tvl_series[-1]
    latest_ts: int = latest_entry["date"]
    latest_tvl: float = latest_entry.get("totalLiquidityUSD", 0.0)

    cutoff_ts = latest_ts - lookback_days * 86_400  # seconds in a day
    past_points = [p for p in tvl_series if p["date"] <= cutoff_ts]
    past_tvl = past_points[-1]["totalLiquidityUSD"] if past_points else None
    tvl_pct = ((latest_tvl - past_tvl) / past_tvl * 100) if past_tvl else None

    # ---------------- Fees / Revenue ----------------
    fees_sum = rev_sum = None
    try:
        fees_json = _fetch_json(f"/summary/fees/{slug}")
        fees_chart = fees_json.get("totalDataChart", [])
        rev_chart = fees_json.get("revenueDataChart", [])

        def _sum_last(chart):
            if not chart:
                return None
            cutoff = chart[-1][0] - lookback_days * 86_400
            return sum(point[1] for point in chart if point[0] >= cutoff)

        fees_sum = _sum_last(fees_chart)
        rev_sum = _sum_last(rev_chart)
    except Exception:
        # Some protocols or chains won’t have fee data; ignore.
        pass

    latest_date = datetime.datetime.utcfromtimestamp(latest_ts).strftime("%Y-%m-%d")

    # ---------------- Assemble markdown ----------------
    lines = [
        f"### {nice_name or slug.upper()} Fundamentals (as of {latest_date})\n",
        f"- **Latest TVL:** ${latest_tvl:,.0f}",
    ]
    if tvl_pct is not None:
        lines.append(f"- **TVL Δ {lookback_days}d:** {tvl_pct:+.2f}%")
    if fees_sum is not None:
        lines.append(f"- **Fees collected ({lookback_days}d):** ${fees_sum:,.0f}")
    if rev_sum is not None:
        lines.append(f"- **Revenue ({lookback_days}d):** ${rev_sum:,.0f}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Quick‑check (only runs when executed directly)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(get_fundamentals("UNI"))






