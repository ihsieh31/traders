from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Tuple


class AnalysisCoverageError(RuntimeError):
    """A selected analyst failed or produced no report; the run must stop."""


SELECTED_ANALYST_REPORT_KEYS: Dict[str, str] = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
    "macro": "macro_report",
}


def validate_selected_analyst_coverage(
    state: dict,
    selected_analysts: list[str],
) -> None:
    """Fail closed when any selected analyst lacks a completed report.

    Only the analysts actually selected for this run are required. A missing
    status, a non-completed status, or an empty report each raise the single
    AnalysisCoverageError; the exception propagates so the existing run
    logger / long-run stop machinery records the round as stopped.
    """
    statuses = state.get("analysis_status") or {}
    for name in selected_analysts:
        report_key = SELECTED_ANALYST_REPORT_KEYS.get(name)
        if report_key is None:
            raise AnalysisCoverageError(
                f"unknown selected analyst '{name}' has no report mapping"
            )
        status = statuses.get(name)
        if status != "completed":
            raise AnalysisCoverageError(
                f"selected analyst '{name}' did not complete (status={status!r})"
            )
        report = state.get(report_key)
        if not isinstance(report, str) or not report.strip():
            raise AnalysisCoverageError(
                f"selected analyst '{name}' returned an empty '{report_key}' report"
            )


REPORT_SPECS: List[Tuple[str, str]] = [
    ("macro_report", "Macro"),
    ("market_report", "Market"),
    ("sentiment_report", "Social Sentiment"),
    ("news_report", "News"),
    ("fundamentals_report", "Fundamentals"),
]


DEFAULT_CONTEXT_CONFIG = {
    "report_context_budget_tokens": 5500,
    "report_context_max_chunks": 16,
    "report_context_min_chunks_per_report": 1,
    "report_context_chunk_chars": 900,
    "report_context_chunk_overlap": 120,
    "report_context_max_points_per_report": 8,
    "report_context_point_chars": 220,
    "report_context_excerpt_chars": 420,
    "report_context_memory_chars": 12000,
    "report_context_compact_points_per_report": 3,
    "report_context_compact_point_chars": 180,
    "report_context_compact_excerpt_chars": 240,
    "report_context_compact_max_excerpts": 8,
    "report_context_max_claims_per_report": 8,
    "report_context_claim_chars": 260,
    "report_context_scoreboard_claims_per_side": 3,
    "debate_digest_max_messages": 6,
    "debate_digest_message_chars": 520,
    "debate_digest_total_chars": 2600,
}


SOURCE_PROFILE: Dict[str, Dict[str, Any]] = {
    "macro_report": {
        "source_type": "macro",
        "source_quality": 0.35,
    },
    "market_report": {
        "source_type": "technical",
        "source_quality": 0.35,
    },
    "sentiment_report": {
        "source_type": "sentiment",
        "source_quality": 0.35,
    },
    "news_report": {
        "source_type": "news",
        "source_quality": 0.35,
    },
    "fundamentals_report": {
        "source_type": "fundamental",
        "source_quality": 0.35,
    },
}


BULLISH_TERMS = (
    "accumulation",
    "beat",
    "breakout",
    "bullish",
    "buy",
    "demand",
    "expand",
    "growth",
    "higher",
    "improve",
    "long",
    "momentum",
    "outperform",
    "raise",
    "rebound",
    "recover",
    "resilient",
    "risk-on",
    "strong",
    "support",
    "upgrade",
    "upside",
    "uptrend",
)


BEARISH_TERMS = (
    "bearish",
    "breakdown",
    "caution",
    "decline",
    "decelerate",
    "downgrade",
    "downside",
    "downtrend",
    "fall",
    "headwind",
    "lower",
    "miss",
    "pressure",
    "resistance",
    "risk",
    "risk-off",
    "sell",
    "short",
    "slow",
    "stop",
    "threat",
    "volatility",
    "weak",
)


ACTIONABILITY_TERMS = (
    "atr",
    "breakout",
    "breakdown",
    "catalyst",
    "entry",
    "exit",
    "guidance",
    "invalidation",
    "level",
    "margin",
    "position",
    "resistance",
    "revenue",
    "risk",
    "rsi",
    "stop",
    "support",
    "target",
    "trend",
    "volume",
)


ROLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "bull_researcher": {
        "fundamentals_report": 1.35,
        "market_report": 1.30,
        "news_report": 1.15,
        "macro_report": 1.00,
        "sentiment_report": 1.10,
    },
    "bear_researcher": {
        "macro_report": 1.30,
        "news_report": 1.20,
        "fundamentals_report": 1.20,
        "market_report": 1.10,
        "sentiment_report": 1.10,
    },
    "research_manager": {
        "macro_report": 1.20,
        "market_report": 1.20,
        "sentiment_report": 1.15,
        "news_report": 1.15,
        "fundamentals_report": 1.20,
    },
    "trader": {
        "market_report": 1.45,
        "macro_report": 1.25,
        "news_report": 1.20,
        "sentiment_report": 1.20,
        "fundamentals_report": 1.10,
    },
    "risky_debator": {
        "market_report": 1.35,
        "sentiment_report": 1.25,
        "news_report": 1.20,
        "fundamentals_report": 1.10,
        "macro_report": 1.05,
    },
    "safe_debator": {
        "macro_report": 1.40,
        "fundamentals_report": 1.25,
        "news_report": 1.20,
        "market_report": 1.10,
        "sentiment_report": 1.05,
    },
    "neutral_debator": {
        "macro_report": 1.20,
        "market_report": 1.20,
        "news_report": 1.20,
        "fundamentals_report": 1.20,
        "sentiment_report": 1.15,
    },
    "risk_manager": {
        "macro_report": 1.35,
        "market_report": 1.30,
        "fundamentals_report": 1.25,
        "news_report": 1.20,
        "sentiment_report": 1.10,
    },
    "default": {
        "macro_report": 1.20,
        "market_report": 1.20,
        "sentiment_report": 1.15,
        "news_report": 1.15,
        "fundamentals_report": 1.20,
    },
}

ROLE_WEIGHTS.update(
    {
        "researchers/bull_researcher": ROLE_WEIGHTS["bull_researcher"],
        "researchers/bear_researcher": ROLE_WEIGHTS["bear_researcher"],
        "managers/research_manager": ROLE_WEIGHTS["research_manager"],
        "managers/risk_manager": ROLE_WEIGHTS["risk_manager"],
    }
)


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "over",
    "under",
    "your",
    "you",
    "are",
    "was",
    "were",
    "have",
    "has",
    "had",
    "about",
    "after",
    "before",
    "also",
    "their",
    "them",
    "they",
    "will",
    "would",
    "should",
    "could",
    "just",
    "than",
    "then",
    "when",
    "where",
    "while",
    "what",
    "which",
    "whose",
    "been",
    "being",
    "into",
    "across",
    "between",
    "current",
    "latest",
    "analysis",
    "report",
    "reports",
    "context",
}


def _get_context_config(config: Dict[str, Any] | None) -> Dict[str, Any]:
    merged = DEFAULT_CONTEXT_CONFIG.copy()
    if config:
        merged.update(config)
    return merged


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def normalize_for_prompt(text: Any) -> str:
    """Normalize any value for prompt injection without truncation."""
    return _normalize_text(text)


def truncate_for_prompt(text: Any, max_chars: int = 1200) -> str:
    """Backward-compatible alias for prompt normalization."""
    _ = max_chars
    return normalize_for_prompt(text)


def _classify_signal(point: str) -> str:
    lower = point.lower()
    bullish_hits = 0
    bearish_hits = 0

    for term in BULLISH_TERMS:
        if term in lower:
            bullish_hits += 1
    for term in BEARISH_TERMS:
        if term in lower:
            bearish_hits += 1

    if bullish_hits > bearish_hits:
        return "Bullish"
    if bearish_hits > bullish_hits:
        return "Bearish"
    return "Mixed"


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, value))


def _round_score(value: float) -> float:
    return round(_clamp_score(value), 2)


def _report_code(report_key: str) -> str:
    return report_key.replace("_report", "")


def _source_profile(report_key: str) -> Dict[str, Any]:
    return SOURCE_PROFILE.get(
        report_key,
        {
            "source_type": "unknown",
            "source_quality": 0.35,
        },
    )


def _split_claim_prefix(point: str) -> Tuple[str, str]:
    if ":" not in point:
        return "Overview", point.strip()

    section_title, claim = point.split(":", 1)
    section_title = section_title.strip() or "Overview"
    claim = claim.strip() or point.strip()
    return section_title, claim


def _parse_date_parts(year: str, month: str, day: str) -> datetime | None:
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _parse_trade_date(value: Any) -> datetime | None:
    text = _normalize_text(value)
    if not text:
        return None

    iso_match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso_match:
        return _parse_date_parts(*iso_match.groups())

    us_match = re.search(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text)
    if us_match:
        month, day, year = us_match.groups()
        return _parse_date_parts(year, month, day)

    return None


def _extract_explicit_dates(text: str) -> List[datetime]:
    dates: List[datetime] = []

    for year, month, day in re.findall(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text):
        parsed = _parse_date_parts(year, month, day)
        if parsed:
            dates.append(parsed)

    for month, day, year in re.findall(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b", text):
        parsed = _parse_date_parts(year, month, day)
        if parsed:
            dates.append(parsed)

    return sorted(dates, reverse=True)


def _score_freshness(text: str, trade_date: datetime | None) -> Tuple[float, str | None, str]:
    explicit_dates = _extract_explicit_dates(text)
    if explicit_dates:
        if trade_date:
            dated = [date for date in explicit_dates if date <= trade_date]
            if not dated:
                return 0.0, explicit_dates[-1].date().isoformat(), "future_event_not_publication"
            selected = dated[0]
            age_days = max(0, (trade_date.date() - selected.date()).days)
        else:
            selected = explicit_dates[0]
            age_days = 0

        if age_days <= 2:
            score = 0.98
        elif age_days <= 7:
            score = 0.91
        elif age_days <= 30:
            score = 0.76
        elif age_days <= 90:
            score = 0.55
        elif age_days <= 180:
            score = 0.36
        else:
            score = 0.20
        return score, selected.date().isoformat(), "explicit"

    # Wording is not publication metadata. Never invent a source timestamp.
    return 0.0, None, "publication_time_unverified"


def _extract_numeric_support(text: str, max_items: int = 4) -> List[str]:
    candidates = re.split(r"\n|;|\|", text)
    support: List[str] = []
    seen = set()

    for candidate in candidates:
        compact = candidate.strip(" -*")
        if not compact or not re.search(r"\d", compact):
            continue
        key = re.sub(r"\W+", "", compact.lower())
        if key in seen:
            continue
        seen.add(key)
        support.append(_truncate(compact, 120))
        if len(support) >= max_items:
            break

    if support:
        return support

    numeric_tokens = re.findall(
        r"(?<!\w)(?:\$?\d+(?:,\d{3})*(?:\.\d+)?%?|[+-]\d+(?:\.\d+)?%|\d+(?:\.\d+)?x)(?!\w)",
        text,
    )
    if not numeric_tokens:
        return []
    return [", ".join(numeric_tokens[:max_items])]


def _score_numeric_support(text: str, numeric_support: List[str]) -> float:
    if not numeric_support:
        return 0.0

    score = min(0.75, 0.22 * len(numeric_support))
    if re.search(r"[%$]|\b\d+(?:\.\d+)?x\b", text):
        score += 0.18
    if any(
        keyword in text.lower()
        for keyword in (
            "revenue",
            "margin",
            "eps",
            "earnings",
            "volume",
            "yield",
            "cpi",
            "atr",
            "rsi",
            "support",
            "resistance",
            "target",
            "stop",
        )
    ):
        score += 0.12
    return _clamp_score(score)


def _score_actionability(text: str, source_type: str, numeric_score: float) -> float:
    lower = text.lower()
    hits = sum(1 for term in ACTIONABILITY_TERMS if term in lower)
    score = 0.18 + min(0.45, hits * 0.07) + numeric_score * 0.25
    if source_type == "technical":
        score += 0.16
    elif source_type in {"fundamental", "macro"}:
        score += 0.08
    return _clamp_score(score)


def _infer_horizon(text: str, source_type: str) -> str:
    lower = text.lower()
    if any(term in lower for term in ("1h", "4h", "swing", "entry", "stop", "target", "atr")):
        return "swing"
    if any(term in lower for term in ("earnings", "guidance", "catalyst", "event")):
        return "event"
    if source_type == "macro":
        return "macro"
    if source_type == "technical":
        return "swing"
    return "position"


def _claim_terms(text: str) -> set[str]:
    generic_terms = {
        "bullish",
        "bearish",
        "mixed",
        "strong",
        "weak",
        "higher",
        "lower",
        "risk",
        "risks",
        "latest",
        "current",
    }
    return {
        term
        for term in _extract_terms(text)
        if term not in generic_terms and len(term) >= 4
    }


def _initial_claim_scores(
    claim_text: str,
    source_quality: float,
    source_type: str,
    freshness_score: float,
    numeric_support: List[str],
) -> Dict[str, float]:
    numeric_score = _score_numeric_support(claim_text, numeric_support)
    actionability_score = _score_actionability(claim_text, source_type, numeric_score)
    priority_score = min(1.0, _line_priority(claim_text) / 12.0)
    evidence_strength = _clamp_score(
        0.30
        + priority_score * 0.20
        + numeric_score * 0.20
        + source_quality * 0.15
        + actionability_score * 0.10
        + freshness_score * 0.05
    )

    return {
        "evidence_strength": evidence_strength,
        "freshness": freshness_score,
        "source_quality": source_quality,
        "numeric_support": numeric_score,
        "contradiction": 0.0,
        "actionability": actionability_score,
        "composite": 0.0,
    }


def _finalize_claim_score(claim: Dict[str, Any]) -> None:
    scores = claim["scores"]
    raw_score = (
        scores["evidence_strength"] * 0.32
        + scores["freshness"] * 0.18
        + scores["source_quality"] * 0.18
        + scores["numeric_support"] * 0.16
        + scores["actionability"] * 0.16
    )
    composite = _clamp_score(raw_score * (1.0 - scores["contradiction"] * 0.35))
    scores["composite"] = composite
    for key, value in list(scores.items()):
        scores[key] = _round_score(value)
    claim["confidence"] = scores["composite"]


def _build_claim_from_point(
    report_key: str,
    report_label: str,
    point: str,
    claim_index: int,
    section_chunks: Dict[Tuple[str, str], List[str]],
    fallback_chunk_ids: List[str],
    trade_date: datetime | None,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    claim_chars = int(config["report_context_claim_chars"])
    section_title, claim_text = _split_claim_prefix(point)
    profile = _source_profile(report_key)
    source_type = profile["source_type"]
    source_quality = float(profile["source_quality"])
    freshness_score, timestamp, timestamp_source = _score_freshness(claim_text, trade_date)
    numeric_support = _extract_numeric_support(claim_text)
    evidence_refs = section_chunks.get((report_key, section_title), [])[:2]
    if not evidence_refs:
        evidence_refs = fallback_chunk_ids[:2]

    scores = _initial_claim_scores(
        claim_text,
        source_quality,
        source_type,
        freshness_score,
        numeric_support,
    )

    return {
        "claim_id": f"{_report_code(report_key)}_{claim_index:03d}",
        "claim": _truncate(claim_text, claim_chars),
        "direction": _classify_signal(claim_text).lower(),
        "source_report": report_key,
        "source_label": report_label,
        "source_type": source_type,
        "section_title": section_title,
        "horizon": _infer_horizon(claim_text, source_type),
        "timestamp": timestamp,
        "timestamp_source": timestamp_source,
        "evidence_refs": evidence_refs,
        "numeric_support": numeric_support,
        "scores": scores,
        "confidence": 0.0,
    }


def _apply_contradiction_scores(claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    major: List[Dict[str, Any]] = []
    directional_claims = [
        claim for claim in claims if claim.get("direction") in {"bullish", "bearish"}
    ]

    for left_index, left in enumerate(directional_claims):
        left_terms = _claim_terms(left["claim"])
        for right in directional_claims[left_index + 1 :]:
            if left["direction"] == right["direction"]:
                continue

            right_terms = _claim_terms(right["claim"])
            overlap = sorted(left_terms & right_terms)
            if not overlap:
                continue

            score = 0.24 + min(0.42, len(overlap) * 0.08)
            if left["source_type"] == right["source_type"]:
                score += 0.10
            score = _clamp_score(score)

            left["scores"]["contradiction"] = max(
                left["scores"]["contradiction"],
                score,
            )
            right["scores"]["contradiction"] = max(
                right["scores"]["contradiction"],
                score,
            )

            if score >= 0.32:
                major.append(
                    {
                        "claim_a": left["claim_id"],
                        "claim_b": right["claim_id"],
                        "score": _round_score(score),
                        "shared_terms": overlap[:5],
                        "reason": "Opposing claims share material terms and should be adjudicated before relying on either side.",
                    }
                )

    for claim in claims:
        _finalize_claim_score(claim)

    major.sort(key=lambda item: item["score"], reverse=True)
    return major[:6]


def _side_score(claims: List[Dict[str, Any]], direction: str) -> float:
    side_claims = sorted(
        [claim for claim in claims if claim.get("direction") == direction],
        key=lambda item: item.get("confidence", 0.0),
        reverse=True,
    )
    if not side_claims:
        return 0.0

    top_claims = side_claims[:5]
    average_confidence = sum(claim["confidence"] for claim in top_claims) / len(top_claims)
    source_diversity = 0.0  # Report roles are not independent primary sources.
    average_contradiction = (
        sum(claim["scores"]["contradiction"] for claim in top_claims) / len(top_claims)
    )
    return _round_score(average_confidence + source_diversity * 0.10 - average_contradiction * 0.08)


def _confidence_label(winning_score: float, score_delta: float, contradiction_score: float) -> str:
    if winning_score >= 0.72 and score_delta >= 0.20 and contradiction_score <= 0.30:
        return "high"
    if winning_score >= 0.55 and score_delta >= 0.08 and contradiction_score <= 0.55:
        return "medium"
    return "low"


def _build_evidence_scoreboard(
    claims: List[Dict[str, Any]],
    major_contradictions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not claims:
        return {
            "schema_version": "1.0",
            "claim_count": 0,
            "bullish_score": 0.0,
            "bearish_score": 0.0,
            "mixed_score": 0.0,
            "net_direction": "mixed",
            "net_confidence": "low",
            "contradiction_score": 0.0,
            "freshness_score": 0.0,
            "source_diversity_score": 0.0,
            "quantitative_score": 0.0,
            "key_bullish_claim_ids": [],
            "key_bearish_claim_ids": [],
            "major_contradictions": [],
            "by_source": {},
            "manager_guidance": "No structured claims were available; ask for more evidence before making a decisive call.",
        }

    bullish_score = _side_score(claims, "bullish")
    bearish_score = _side_score(claims, "bearish")
    mixed_claims = [claim for claim in claims if claim.get("direction") == "mixed"]
    mixed_score = (
        _round_score(sum(claim["confidence"] for claim in mixed_claims) / len(mixed_claims))
        if mixed_claims
        else 0.0
    )
    score_delta = abs(bullish_score - bearish_score)

    if bullish_score - bearish_score >= 0.08:
        net_direction = "bullish"
        winning_score = bullish_score
    elif bearish_score - bullish_score >= 0.08:
        net_direction = "bearish"
        winning_score = bearish_score
    else:
        net_direction = "mixed"
        winning_score = max(bullish_score, bearish_score, mixed_score)

    contradiction_score = _round_score(
        sum(claim["scores"]["contradiction"] for claim in claims) / len(claims)
    )
    freshness_score = _round_score(
        sum(claim["scores"]["freshness"] for claim in claims) / len(claims)
    )
    quantitative_score = _round_score(
        sum(claim["scores"]["numeric_support"] for claim in claims) / len(claims)
    )
    source_diversity_score = 0.0  # Independent sources are not verified by report roles.

    key_bullish = [
        claim["claim_id"]
        for claim in sorted(
            [claim for claim in claims if claim.get("direction") == "bullish"],
            key=lambda item: item.get("confidence", 0.0),
            reverse=True,
        )[:3]
    ]
    key_bearish = [
        claim["claim_id"]
        for claim in sorted(
            [claim for claim in claims if claim.get("direction") == "bearish"],
            key=lambda item: item.get("confidence", 0.0),
            reverse=True,
        )[:3]
    ]

    by_source: Dict[str, Dict[str, Any]] = {}
    for report_key, report_label in REPORT_SPECS:
        source_claims = [claim for claim in claims if claim["source_report"] == report_key]
        if not source_claims:
            continue
        direction_scores = {
            direction: _round_score(
                sum(
                    claim["confidence"]
                    for claim in source_claims
                    if claim["direction"] == direction
                )
            )
            for direction in ("bullish", "bearish", "mixed")
        }
        dominant_direction = max(direction_scores, key=direction_scores.get)
        by_source[report_key] = {
            "label": report_label,
            "dominant_direction": dominant_direction,
            "scores": direction_scores,
        }

    if net_direction == "mixed":
        guidance = "Evidence is mixed; manager should prefer HOLD/NEUTRAL unless execution evidence clearly resolves contradictions."
    elif contradiction_score >= 0.45:
        guidance = f"{net_direction.title()} evidence leads, but contradiction is elevated; manager should discount weak or stale claims."
    else:
        guidance = f"{net_direction.title()} evidence currently has the stronger scored packet; manager should verify key counterclaims before deciding."

    return {
        "schema_version": "1.0",
        "claim_count": len(claims),
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "mixed_score": mixed_score,
        "net_direction": net_direction,
        "net_confidence": _confidence_label(winning_score, score_delta, contradiction_score),
        "contradiction_score": contradiction_score,
        "freshness_score": freshness_score,
        "source_diversity_score": source_diversity_score,
        "quantitative_score": quantitative_score,
        "key_bullish_claim_ids": key_bullish,
        "key_bearish_claim_ids": key_bearish,
        "major_contradictions": major_contradictions,
        "by_source": by_source,
        "manager_guidance": guidance,
    }


def _build_evidence_layer(
    context: Dict[str, Any],
    state: Dict[str, Any],
    config: Dict[str, Any],
) -> None:
    trade_date = _parse_trade_date(state.get("trade_date"))
    max_claims_per_report = int(config["report_context_max_claims_per_report"])
    section_chunks: Dict[Tuple[str, str], List[str]] = {}

    for chunk in context.get("chunks", []):
        key = (chunk["report_key"], chunk["section_title"])
        section_chunks.setdefault(key, []).append(chunk["id"])

    claims: List[Dict[str, Any]] = []
    for report_key, report_label in REPORT_SPECS:
        report_meta = context.get("reports", {}).get(report_key)
        if not report_meta:
            continue

        for claim_index, point in enumerate(
            report_meta.get("coverage_points", [])[:max_claims_per_report],
            start=1,
        ):
            claims.append(
                _build_claim_from_point(
                    report_key,
                    report_label,
                    point,
                    claim_index,
                    section_chunks,
                    report_meta.get("chunk_ids", []),
                    trade_date,
                    config,
                )
            )

    major_contradictions = _apply_contradiction_scores(claims)
    scoreboard = _build_evidence_scoreboard(claims, major_contradictions)

    context["evidence_claims"] = claims
    context["evidence_scoreboard"] = scoreboard
    context["stats"]["total_claims"] = len(claims)
    context["stats"]["bullish_claims"] = sum(
        1 for claim in claims if claim.get("direction") == "bullish"
    )
    context["stats"]["bearish_claims"] = sum(
        1 for claim in claims if claim.get("direction") == "bearish"
    )
    context["stats"]["mixed_claims"] = sum(
        1 for claim in claims if claim.get("direction") == "mixed"
    )


def _claim_lookup(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {claim["claim_id"]: claim for claim in context.get("evidence_claims", [])}


def _render_claim_line(claim: Dict[str, Any], max_chars: int) -> str:
    scores = claim.get("scores", {})
    return (
        f"[{claim['claim_id']} {claim['direction']} priority={claim.get('confidence', 0):.2f} "
        f"date_hint={scores.get('freshness', 0):.2f} numeric_hint={scores.get('numeric_support', 0):.2f} "
        f"overlap_hint={scores.get('contradiction', 0):.2f}] "
        f"{_truncate(claim.get('claim', ''), max_chars)}"
    )


def _render_evidence_scoreboard(
    context: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> str:
    cfg = _get_context_config(config)
    claim_chars = int(cfg["report_context_compact_point_chars"])
    max_claims = int(cfg["report_context_scoreboard_claims_per_side"])
    scoreboard = context.get("evidence_scoreboard", {})
    claims_by_id = _claim_lookup(context)

    if not scoreboard or not context.get("evidence_claims"):
        return "Heuristic Claim Reading Guide: No structured claims available."

    lines: List[str] = []
    lines.append("Heuristic Claim Reading Guide:")

    for label, key in (
        ("Bullish-tagged excerpts (heuristic labels)", "key_bullish_claim_ids"),
        ("Bearish-tagged excerpts (heuristic labels)", "key_bearish_claim_ids"),
    ):
        claim_ids = scoreboard.get(key, [])[:max_claims]
        if not claim_ids:
            continue
        lines.append(f"- {label}:")
        for claim_id in claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim:
                lines.append(f"  - {_render_claim_line(claim, claim_chars)}")

    contradictions = scoreboard.get("major_contradictions", [])[:3]
    if contradictions:
        lines.append("- Potential claim overlaps to review (not verified contradictions):")
        for item in contradictions:
            shared_terms = ", ".join(item.get("shared_terms", [])) or "shared evidence terms"
            lines.append(
                f"  - [{item['claim_a']}] vs [{item['claim_b']}] "
                f"overlap_score={item.get('score', 0):.2f}; overlap: {shared_terms}"
            )

    return "\n".join(lines).strip()


def _render_decision_claim_matrix(
    context: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> str:
    cfg = _get_context_config(config)
    max_points = int(cfg["report_context_compact_points_per_report"])
    point_chars = int(cfg["report_context_compact_point_chars"])

    lines: List[str] = []
    lines.append("Heuristic Claim Priority Matrix (reading-order heuristic; NOT probability, confidence, win rate or verified evidence):")
    lines.append("Dates and numbers below are claims in generated reports, not independently verified facts. Repeated coverage across roles is not independent corroboration. Resolve claims against the linked original source; missing publication dates remain unknown.")
    lines.append("The priority score is only a reading-order heuristic. It is not source verification, model confidence, probability, win rate or an independent vote. Numeric-looking text may still be wrong. Inspect the supplied excerpt, source label and as-of date before using a claim. Never assign high confidence solely because this score is high.")
    lines.append("Direction labels are keyword heuristics, not trade recommendations. Overlap hints may reflect compatible facts or different horizons, not factual contradictions.")
    lines.append(_render_evidence_scoreboard(context, config=config))
    lines.append("")
    lines.append("Claims by source:")

    claims_by_report: Dict[str, List[Dict[str, Any]]] = {}
    for claim in context.get("evidence_claims", []):
        claims_by_report.setdefault(claim["source_report"], []).append(claim)

    for report_key, _ in REPORT_SPECS:
        report_meta = context.get("reports", {}).get(report_key)
        if not report_meta:
            continue

        report_claims = sorted(
            claims_by_report.get(report_key, []),
            key=lambda item: item.get("confidence", 0.0),
            reverse=True,
        )[:max_points]
        if report_claims:
            rendered_points: List[str] = []
            for claim in report_claims:
                rendered_points.append(_render_claim_line(claim, point_chars))

            joined_points = " | ".join(rendered_points)
            lines.append(f"- {report_meta['label']}: {joined_points}")
            continue

        points = report_meta.get("coverage_points", [])[:max_points]
        if not points:
            lines.append(f"- {report_meta['label']}: No usable claims.")
            continue

        rendered_points: List[str] = []
        for point in points:
            rendered_points.append(_truncate(point, point_chars))

        joined_points = " | ".join(rendered_points)
        lines.append(f"- {report_meta['label']}: {joined_points}")

    return "\n".join(lines).strip()


def _split_sections(text: str) -> List[Tuple[str, str]]:
    if not text:
        return []

    sections: List[Tuple[str, str]] = []
    current_title = "Overview"
    current_lines: List[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+\S+", stripped):
            body = "\n".join(current_lines).strip()
            if body:
                sections.append((current_title, body))
            current_title = re.sub(r"^#{1,6}\s*", "", stripped).strip()
            current_lines = []
            continue
        current_lines.append(line)

    body = "\n".join(current_lines).strip()
    if body:
        sections.append((current_title, body))

    if not sections:
        sections.append(("Overview", text))

    return sections


def _is_table_separator(line: str) -> bool:
    compact = line.replace("|", "").replace("-", "").replace(":", "").replace(" ", "")
    return compact == ""


def _extract_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if len(line) < 20:
            continue
        if _is_table_separator(line):
            continue
        line = re.sub(r"^\s*[-*]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        if len(line) < 20:
            continue
        candidates.append(line)
    return candidates


def _line_priority(line: str) -> int:
    lower = line.lower()
    score = 0
    if any(ch.isdigit() for ch in line):
        score += 3
    if "%" in line or "$" in line:
        score += 3
    if any(
        keyword in lower
        for keyword in (
            "risk",
            "stop",
            "target",
            "entry",
            "exit",
            "trend",
            "support",
            "resistance",
            "atr",
            "rsi",
            "macd",
            "earnings",
            "guidance",
            "revenue",
            "cpi",
            "fomc",
            "yield",
            "position",
            "volatility",
            "sentiment",
        )
    ):
        score += 3
    if ":" in line:
        score += 1
    return score


def _extract_coverage_points(
    section_title: str,
    section_text: str,
    max_points: int,
    point_chars: int,
) -> List[str]:
    candidates = _extract_candidates(section_text)
    ranked = sorted(
        candidates,
        key=lambda item: (_line_priority(item), len(item)),
        reverse=True,
    )

    seen = set()
    points: List[str] = []

    for candidate in ranked:
        compact = re.sub(r"\W+", "", candidate.lower())
        if not compact or compact in seen:
            continue
        seen.add(compact)
        points.append(f"{section_title}: {_truncate(candidate, point_chars)}")
        if len(points) >= max_points:
            break

    if points:
        return points

    fallback = _truncate(section_text.replace("\n", " ").strip(), point_chars)
    if not fallback:
        return []
    return [f"{section_title}: {fallback}"]


def _chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: List[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(text_len, start + max_chars)
        if end < text_len:
            split_newline = text.rfind("\n", start + int(max_chars * 0.6), end)
            split_space = text.rfind(" ", start + int(max_chars * 0.6), end)
            split = max(split_newline, split_space)
            if split > start:
                end = split

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_len:
            break

        start = max(0, end - overlap)

    return chunks


def _extract_terms(text: str) -> List[str]:
    terms: List[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_/%\.-]{2,}", text.lower()):
        if token in STOPWORDS:
            continue
        terms.append(token)
    return terms


def _score_chunk(
    chunk: Dict[str, Any],
    query_terms: List[str],
    role_weights: Dict[str, float],
) -> float:
    score = role_weights.get(chunk["report_key"], 1.0)
    text_lower = chunk["text"].lower()

    if query_terms:
        overlap = 0
        for term in query_terms:
            if term in text_lower:
                overlap += 1
        score += overlap * 2.0
        if overlap == 0:
            score -= 0.3

    if any(k in text_lower for k in ("buy", "sell", "hold", "long", "short", "risk")):
        score += 0.5
    if any(ch.isdigit() for ch in chunk["text"]):
        score += 0.2

    return score


def build_report_context_index(
    state: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    cfg = _get_context_config(config)
    max_points = int(cfg["report_context_max_points_per_report"])
    point_chars = int(cfg["report_context_point_chars"])
    chunk_chars = int(cfg["report_context_chunk_chars"])
    overlap = int(cfg["report_context_chunk_overlap"])

    context: Dict[str, Any] = {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reports": {},
        "chunks": [],
        "evidence_claims": [],
        "evidence_scoreboard": {},
        "report_order": [spec[0] for spec in REPORT_SPECS],
        "stats": {
            "reports_with_content": 0,
            "total_chunks": 0,
            "total_claims": 0,
            "total_tokens_estimate": 0,
            "total_chars": 0,
        },
    }

    global_overview_lines: List[str] = []

    for report_key, report_label in REPORT_SPECS:
        raw_text = _normalize_text(state.get(report_key, ""))
        if not raw_text:
            continue

        sections = _split_sections(raw_text)
        coverage_points: List[str] = []
        report_chunk_ids: List[str] = []

        for sec_idx, (section_title, section_text) in enumerate(sections, start=1):
            coverage_points.extend(
                _extract_coverage_points(
                    section_title,
                    section_text,
                    max_points=max(1, max_points // 3),
                    point_chars=point_chars,
                )
            )
            for chunk_idx, chunk_text in enumerate(
                _chunk_text(section_text, chunk_chars, overlap),
                start=1,
            ):
                chunk_id = (
                    f"{report_key.replace('_report', '')}"
                    f"_s{sec_idx}_c{chunk_idx}"
                )
                chunk_payload = {
                    "id": chunk_id,
                    "report_key": report_key,
                    "report_label": report_label,
                    "section_title": section_title,
                    "text": chunk_text,
                    "token_estimate": _estimate_tokens(chunk_text),
                    "char_count": len(chunk_text),
                }
                context["chunks"].append(chunk_payload)
                report_chunk_ids.append(chunk_id)

        # Keep only the top coverage points per report.
        coverage_points = coverage_points[:max_points]
        summary = "\n".join(f"- {point}" for point in coverage_points)

        context["reports"][report_key] = {
            "label": report_label,
            "char_count": len(raw_text),
            "token_estimate": _estimate_tokens(raw_text),
            "coverage_points": coverage_points,
            "summary": summary,
            "chunk_ids": report_chunk_ids,
        }

        first_point = coverage_points[0] if coverage_points else _truncate(raw_text, 140)
        global_overview_lines.append(f"- {report_label}: {first_point}")

        context["stats"]["reports_with_content"] += 1
        context["stats"]["total_tokens_estimate"] += _estimate_tokens(raw_text)
        context["stats"]["total_chars"] += len(raw_text)

    context["stats"]["total_chunks"] = len(context["chunks"])
    context["global_overview"] = "\n".join(global_overview_lines)
    _build_evidence_layer(context, state, cfg)

    return context


def _select_chunks_for_agent(
    context: Dict[str, Any],
    agent_role: str,
    objective: str,
    config: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    cfg = _get_context_config(config)
    token_budget = int(cfg["report_context_budget_tokens"])
    max_chunks = int(cfg["report_context_max_chunks"])
    min_chunks_per_report = int(cfg["report_context_min_chunks_per_report"])

    chunks = context.get("chunks", [])
    if not chunks:
        return []

    query_terms = _extract_terms(objective)
    role_weights = ROLE_WEIGHTS.get(agent_role, ROLE_WEIGHTS["default"])

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for chunk in chunks:
        scored.append((_score_chunk(chunk, query_terms, role_weights), chunk))
    scored.sort(key=lambda item: item[0], reverse=True)

    selected: List[Dict[str, Any]] = []
    selected_ids = set()
    used_tokens = 0

    # Coverage pass: force representation from each report first.
    for report_key, _ in REPORT_SPECS:
        added_for_report = 0
        for score, chunk in scored:
            if chunk["report_key"] != report_key:
                continue
            if chunk["id"] in selected_ids:
                continue
            token_cost = chunk["token_estimate"]
            if used_tokens + token_cost > token_budget:
                continue
            selected.append(chunk)
            selected_ids.add(chunk["id"])
            used_tokens += token_cost
            added_for_report += 1
            if added_for_report >= min_chunks_per_report:
                break

    # Relevance pass: fill remaining budget by score.
    for score, chunk in scored:
        if len(selected) >= max_chunks:
            break
        if chunk["id"] in selected_ids:
            continue
        token_cost = chunk["token_estimate"]
        if used_tokens + token_cost > token_budget:
            continue
        selected.append(chunk)
        selected_ids.add(chunk["id"])
        used_tokens += token_cost

    return selected


def _render_analysis_context(
    context: Dict[str, Any],
    selected_chunks: List[Dict[str, Any]],
    config: Dict[str, Any] | None = None,
) -> str:
    cfg = _get_context_config(config)
    excerpt_chars = int(cfg["report_context_excerpt_chars"])

    lines: List[str] = []
    lines.append("Cross-Analyst Context Packet")
    lines.append(
        "This packet organizes analyst claims with a heuristic claim priority matrix. "
        "The priority score is only a reading-order heuristic. It is not source "
        "verification, model confidence, probability, win rate or an independent vote. "
        "Numeric-looking text may still be wrong."
    )
    lines.append("")

    global_overview = context.get("global_overview", "").strip()
    if global_overview:
        lines.append("Topline Overview:")
        lines.append(global_overview)
        lines.append("")

    evidence_scoreboard = _render_evidence_scoreboard(context, config=config)
    if evidence_scoreboard:
        lines.append(evidence_scoreboard)
        lines.append("")

    lines.append("Full-Coverage Highlights:")
    for report_key, _ in REPORT_SPECS:
        report_meta = context.get("reports", {}).get(report_key)
        if not report_meta:
            continue
        lines.append(f"{report_meta['label']}:")
        points = report_meta.get("coverage_points", [])
        if not points:
            lines.append("- No structured highlights available.")
            continue
        for point in points:
            lines.append(f"- {point}")
    lines.append("")

    if selected_chunks:
        lines.append("Role-Relevant Evidence Excerpts (retrieved by objective):")
        for chunk in selected_chunks:
            excerpt = _truncate(chunk["text"].replace("\n", " "), excerpt_chars)
            lines.append(
                f"[{chunk['id']} | {chunk['report_label']} | {chunk['section_title']}] "
                f"{excerpt}"
            )

    return "\n".join(lines).strip()


def _render_analysis_context_compact(
    context: Dict[str, Any],
    selected_chunks: List[Dict[str, Any]],
    config: Dict[str, Any] | None = None,
) -> str:
    cfg = _get_context_config(config)
    excerpt_chars = int(cfg["report_context_compact_excerpt_chars"])
    max_excerpts = int(cfg["report_context_compact_max_excerpts"])

    lines: List[str] = []
    lines.append("Cross-Analyst Context Packet (Compact)")
    lines.append("Use this compact packet for fast decisioning with full report coverage preserved. The priority matrix is a reading-order heuristic, not confidence, probability or win rate.")
    lines.append("")

    global_overview = context.get("global_overview", "").strip()
    if global_overview:
        lines.append("Topline Overview:")
        lines.append(global_overview)
        lines.append("")

    lines.append(_render_decision_claim_matrix(context, config=config))
    lines.append("")

    if selected_chunks:
        lines.append("Top Evidence Excerpts:")
        for chunk in selected_chunks[:max_excerpts]:
            excerpt = _truncate(chunk["text"].replace("\n", " "), excerpt_chars)
            lines.append(
                f"[{chunk['id']} | {chunk['report_label']} | {chunk['section_title']}] {excerpt}"
            )

    return "\n".join(lines).strip()


def _render_memory_context(
    context: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> str:
    cfg = _get_context_config(config)
    max_chars = int(cfg["report_context_memory_chars"])

    lines: List[str] = []
    lines.append("Condensed cross-report situation context:")

    global_overview = context.get("global_overview", "").strip()
    if global_overview:
        lines.append(global_overview)

    for report_key, _ in REPORT_SPECS:
        report_meta = context.get("reports", {}).get(report_key)
        if not report_meta:
            continue
        lines.append(f"{report_meta['label']} highlights:")
        for point in report_meta.get("coverage_points", [])[:4]:
            lines.append(f"- {point}")

    rendered = "\n".join(lines).strip()
    return _truncate(rendered, max_chars)


def _render_all_reports_text(
    state: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> str:
    """Render full analyst reports only when explicitly enabled (latency heavy)."""
    cfg = _get_context_config(config)
    include_full = bool((config or {}).get("include_full_reports_in_prompts", False))
    if not include_full:
        return "Full raw reports omitted for latency. Use claim matrix + retrieved evidence context."

    lines: List[str] = []
    lines.append("Full Analyst Reports (Untruncated):")
    for report_key, report_label in REPORT_SPECS:
        raw_text = _normalize_text(state.get(report_key, ""))
        if not raw_text:
            continue
        lines.append(f"### {report_label}")
        lines.append(raw_text)
        lines.append("")
    return "\n".join(lines).strip()


def _short_message_line(message: str, label: str, max_chars: int) -> str:
    msg = _normalize_text(message)
    if msg.lower().startswith(label.lower() + ":"):
        msg = msg.split(":", 1)[1].strip()
    return _truncate(msg, max_chars)


def build_debate_digest(
    debate_state: Dict[str, Any] | None,
    debate_type: str,
    config: Dict[str, Any] | None = None,
) -> str:
    """Build a compact digest for either investment or risk debate states."""
    if not isinstance(debate_state, dict):
        return ""

    cfg = _get_context_config(config)
    max_messages = int(cfg["debate_digest_max_messages"])
    msg_chars = int(cfg["debate_digest_message_chars"])
    total_chars = int(cfg["debate_digest_total_chars"])

    lines: List[str] = []
    if debate_type == "investment":
        lines.append("Investment Debate Digest:")
        lines.append(f"- Turn count: {debate_state.get('count', 0)}")
        current = _truncate(normalize_for_prompt(debate_state.get("current_response", "")), msg_chars)
        if current:
            lines.append(f"- Latest response: {current}")

        bull_messages = list(debate_state.get("bull_messages", []))[-max_messages:]
        bear_messages = list(debate_state.get("bear_messages", []))[-max_messages:]
        for message in bull_messages[-max_messages // 2 :]:
            lines.append(f"- Bull: {_short_message_line(message, 'Bull Analyst', msg_chars)}")
        for message in bear_messages[-max_messages // 2 :]:
            lines.append(f"- Bear: {_short_message_line(message, 'Bear Analyst', msg_chars)}")
    else:
        lines.append("Risk Debate Digest:")
        lines.append(f"- Turn count: {debate_state.get('count', 0)}")
        lines.append(f"- Latest speaker: {debate_state.get('latest_speaker', 'Unknown')}")

        latest_map = [
            ("Risky", debate_state.get("current_risky_response", "")),
            ("Safe", debate_state.get("current_safe_response", "")),
            ("Neutral", debate_state.get("current_neutral_response", "")),
        ]
        for label, content in latest_map:
            compact = _truncate(normalize_for_prompt(content), msg_chars)
            if compact:
                lines.append(f"- {label} latest: {compact}")

        msg_sources = [
            ("Risky", list(debate_state.get("risky_messages", []))),
            ("Safe", list(debate_state.get("safe_messages", []))),
            ("Neutral", list(debate_state.get("neutral_messages", []))),
        ]
        per_agent = max(1, max_messages // 3)
        for label, messages in msg_sources:
            for message in messages[-per_agent:]:
                lines.append(f"- {label}: {_short_message_line(message, f'{label} Analyst', msg_chars)}")

    return _truncate("\n".join(lines).strip(), total_chars)


def ensure_report_context(
    state: Dict[str, Any],
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    context = state.get("report_context")
    if (
        isinstance(context, dict)
        and context.get("chunks") is not None
        and context.get("evidence_claims") is not None
        and context.get("evidence_scoreboard") is not None
    ):
        return context
    context = build_report_context_index(state, config=config)
    # Mutating state here is intentional to avoid rebuilding the index in each node.
    state["report_context"] = context
    return context


def get_agent_context_bundle(
    state: Dict[str, Any],
    agent_role: str,
    objective: str,
    config: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    context = ensure_report_context(state, config=config)
    selected_chunks = _select_chunks_for_agent(
        context,
        agent_role=agent_role,
        objective=objective,
        config=config,
    )

    analysis_context = _render_analysis_context(context, selected_chunks, config=config)
    analysis_context_compact = _render_analysis_context_compact(
        context, selected_chunks, config=config
    )
    decision_claim_matrix = _render_decision_claim_matrix(context, config=config)
    evidence_scoreboard = _render_evidence_scoreboard(context, config=config)
    memory_context = _render_memory_context(context, config=config)

    return {
        "analysis_context": analysis_context,
        "analysis_context_compact": analysis_context_compact,
        "decision_claim_matrix": decision_claim_matrix,
        "evidence_scoreboard": evidence_scoreboard,
        "evidence_claims": context.get("evidence_claims", []),
        "evidence_scoreboard_data": context.get("evidence_scoreboard", {}),
        "memory_context": memory_context,
        "all_reports_text": (_render_all_reports_text(state, config=config)
                             if (config or {}).get("include_full_reports_in_prompts", False)
                             else "Retrieved analyst excerpts (not full reports):\n" + analysis_context),
        "selected_chunk_ids": [chunk["id"] for chunk in selected_chunks],
        "context_stats": context.get("stats", {}),
    }


def create_report_context_node(
    config: Dict[str, Any] | None = None,
    selected_analysts: List[str] | None = None,
):
    """Build the shared report-context node.

    ``selected_analysts`` is the actual selection for this graph instance,
    supplied by setup.py at node creation time (never guessed from global
    config). When provided, the node first runs the coverage gate and fails
    closed before any downstream decision agent can consume partial context.
    """

    def report_context_node(state: Dict[str, Any]) -> Dict[str, Any]:
        if selected_analysts:
            validate_selected_analyst_coverage(state, selected_analysts)
        return {"report_context": build_report_context_index(state, config=config)}

    return report_context_node
