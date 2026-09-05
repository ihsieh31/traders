"""Phase C Screening prompt construction and output acceptance.

The Screening role sees exactly one compact factor table per logical
invocation — symbol, price, adv20, r5/r20/r60, vol20, volume_ratio, trend,
the deterministic score and (only when the P2 sector mapping covers every
candidate) the sector. It never sees holdings, cash, buying power, news,
or full-market bars.

Sector diversity: when every candidate carries a sector, at most
``screening_max_per_sector`` (5) selections may share a sector and the
prompt says so up front; if the candidate pool cannot possibly satisfy
that cap (``sum(min(count_by_sector, 5)) < 20``) the round stops with
``INSUFFICIENT_SECTOR_CAPACITY`` instead of quietly relaxing the rule.
When sector metadata is missing for any candidate, diversity is applied
NOWHERE and the omission is recorded explicitly — unknown sectors are
never assumed to be several distinct sectors. This selection-time
degradation never relaxes the P2 execution-time sector cap, which stays
fail-closed on unknown sectors.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from tradingagents.prompts import render_prompt
from tradingagents.screening.llm import (
    ScreenedCandidate,
    ScreeningStop,
    invoke_screening_structured,
)
from tradingagents.screening.metrics import SymbolFeatures

_FACTOR_DECIMALS = {
    "price": 4,
    "adv20": 0,
    "r5": 4,
    "r20": 4,
    "r60": 4,
    "vol20": 4,
    "volume_ratio": 3,
    "trend": 4,
    "score": 2,
}


def build_sector_plan(
    candidates: Sequence[SymbolFeatures],
    *,
    max_per_sector: int,
    select_n: int,
) -> Dict[str, object]:
    """Decide whether sector diversity can be applied to this candidate set."""
    missing = [c.symbol for c in candidates if not (c.sector or "").strip()]
    applied = not missing
    plan = {
        "applied": applied,
        "max_per_sector": int(max_per_sector),
        "missing_sectors": missing,
        "insufficient_capacity": False,
    }
    if applied:
        counts: Dict[str, int] = {}
        for c in candidates:
            counts[c.sector] = counts.get(c.sector, 0) + 1
        capacity = sum(min(n, int(max_per_sector)) for n in counts.values())
        plan["sector_counts"] = counts
        if capacity < select_n:
            plan["insufficient_capacity"] = True
    return plan


def render_compact_table(candidates: Sequence[SymbolFeatures]) -> str:
    """Deterministic CSV-style table with headers and units."""
    header = "symbol,price_usd,adv20_usd,r5,r20,r60,vol20,volume_ratio,trend,score,sector"
    lines = [header]
    for c in candidates:
        sector = c.sector if (c.sector or "").strip() else ""
        lines.append(
            ",".join(
                [
                    c.symbol,
                    f"{c.price:.{_FACTOR_DECIMALS['price']}f}",
                    f"{c.adv20:.{_FACTOR_DECIMALS['adv20']}f}",
                    f"{c.r5:.{_FACTOR_DECIMALS['r5']}f}",
                    f"{c.r20:.{_FACTOR_DECIMALS['r20']}f}",
                    f"{c.r60:.{_FACTOR_DECIMALS['r60']}f}",
                    f"{c.vol20:.{_FACTOR_DECIMALS['vol20']}f}",
                    f"{c.volume_ratio:.{_FACTOR_DECIMALS['volume_ratio']}f}",
                    f"{c.trend:.{_FACTOR_DECIMALS['trend']}f}",
                    f"{(c.score or 0.0):.{_FACTOR_DECIMALS['score']}f}",
                    sector,
                ]
            )
        )
    return "\n".join(lines)


def build_screening_messages(
    candidates: Sequence[SymbolFeatures],
    sector_plan: Dict[str, object],
    *,
    select_n: int,
) -> List[dict]:
    if sector_plan.get("applied"):
        sector_rules = (
            f"- sector: the candidate's sector. SECTOR CONSTRAINT: select at most "
            f"{int(sector_plan['max_per_sector'])} candidates from any single sector."
        )
    else:
        sector_rules = (
            "- sector metadata is unavailable for this candidate set; no sector "
            "constraint applies and none may be inferred."
        )
    system_prompt = render_prompt(
        "screening/screening_selection",
        candidate_count=len(candidates),
        select_n=select_n,
        sector_rules=sector_rules,
    )
    user_prompt = (
        "Candidate factor table (one row per symbol):\n\n"
        f"{render_compact_table(candidates)}\n\n"
        f"Select and rank exactly {select_n} candidates for full research."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def run_screening_invocation(
    screening_llm: Any,
    candidates: Sequence[SymbolFeatures],
    sector_plan: Dict[str, object],
    *,
    select_n: int,
    max_per_sector: int,
) -> List[ScreenedCandidate]:
    """One logical Screening invocation + full strict acceptance."""
    if sector_plan.get("insufficient_capacity"):
        raise ScreeningStop(
            "INSUFFICIENT_SECTOR_CAPACITY",
            "the candidate pool cannot fill "
            f"{select_n} selections under the {max_per_sector}-per-sector cap",
        )
    messages = build_screening_messages(candidates, sector_plan, select_n=select_n)
    validated = invoke_screening_structured(
        screening_llm,
        messages,
        expected_count=select_n,
        input_symbols={c.symbol for c in candidates},
    )

    if sector_plan.get("applied"):
        per_sector: Dict[str, int] = {}
        for c in validated:
            sector = next(f.sector for f in candidates if f.symbol == c.symbol)
            per_sector[sector] = per_sector.get(sector, 0) + 1
        over = {s: n for s, n in per_sector.items() if n > max_per_sector}
        if over:
            raise ScreeningStop(
                "SCREENING_INVALID_OUTPUT",
                f"sector diversity violated (max {max_per_sector}): {over}",
            )
    return validated
