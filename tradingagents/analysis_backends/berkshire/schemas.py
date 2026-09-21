"""Strict, non-executable contracts for Berkshire research outputs."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, TypedDict


class BerkshireRoleReport(TypedDict):
    role: str
    thesis: str
    key_facts: list[str]
    uncertainties: list[str]
    risks: list[str]
    valuation_notes: list[str]
    evidence_refs: list[str]


class BerkshireCanonicalReports(TypedDict):
    market_report: str
    sentiment_report: str
    news_report: str
    fundamentals_report: str
    macro_report: str


ROLE_NAMES = (
    "business_analyst",
    "financial_analyst",
    "industry_researcher",
    "risk_assessor",
)

CANONICAL_REPORT_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "macro_report",
)

# Reject executable instructions and decision fields, while allowing factual
# analysis such as "the company expects to sell twice as many chips" or
# "long-term demand".  The contract bans issuing decisions, not every
# occurrence of an English word that can also appear in source evidence.
_EXECUTABLE_PATTERNS = (
    r"\b(?:recommend(?:ation)?|suggest(?:ion)?|decision|stance|proposal|signal|action)\b"
    r"[^.\n]{0,100}\b(?:buy|sell|long|short|hold|wait|avoid)\b",
    r"\b(?:buy|sell|hold|wait|avoid)\b[^.\n]{0,100}\b"
    r"(?:shares?|stock|position|exposure|entry|allocation|investment|portfolio)\b",
    r"\b(?:go|stay|remain|open|close|enter|exit|reduce|increase)\s+"
    r"(?:long|short|flat|neutral|(?:a|the)\s+(?:long|short)\s+position|"
    r"(?:the\s+)?(?:position|exposure))\b",
    r"\b(?:position\s+sizing|portfolio\s+weight|tradeintent|executable\s+order)\b",
    r"(?:\bdo not\b|\bdon't\b|\bshould not\b|\bmust not\b|\bnever\b)\s+"
    r"(?:buy|sell|hold|wait|avoid|go\s+long|go\s+short)\b",
)
_EXECUTABLE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _EXECUTABLE_PATTERNS)


def assert_analysis_only(value: Any, *, label: str) -> None:
    text = json.dumps(value, ensure_ascii=False, default=str)
    for pattern in _EXECUTABLE:
        match = pattern.search(text)
        if match:
            excerpt = match.group(0).strip()
            raise ValueError(
                f"{label} contains forbidden executable decision language: {excerpt!r}"
            )
    # JSON action-like fields are executable even when the value is separated
    # from the surrounding prose.
    action_field = re.search(
        r'"(?:action|recommendation|signal|final_action|final_decision)"\s*:\s*'
        r'"(?:buy|sell|long|short|hold|wait|avoid)"',
        text,
        flags=re.IGNORECASE,
    )
    if action_field:
        raise ValueError(
            f"{label} contains forbidden executable decision language: {action_field.group(0)!r}"
        )


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"Berkshire report field {field!r} must be a string list")
    return [item.strip() for item in value]


def validate_role_report(value: Any, *, expected_role: str) -> BerkshireRoleReport:
    if not isinstance(value, Mapping):
        raise ValueError(f"{expected_role} report must be an object")
    if value.get("role") != expected_role:
        raise ValueError(
            f"Berkshire role mismatch: expected {expected_role!r}, got {value.get('role')!r}"
        )
    thesis = value.get("thesis")
    if not isinstance(thesis, str) or not thesis.strip():
        raise ValueError(f"{expected_role} report thesis is empty")
    result: BerkshireRoleReport = {
        "role": expected_role,
        "thesis": thesis.strip(),
        "key_facts": _string_list(value.get("key_facts"), "key_facts"),
        "uncertainties": _string_list(value.get("uncertainties"), "uncertainties"),
        "risks": _string_list(value.get("risks"), "risks"),
        "valuation_notes": _string_list(value.get("valuation_notes"), "valuation_notes"),
        "evidence_refs": _string_list(value.get("evidence_refs"), "evidence_refs"),
    }
    assert_analysis_only(result, label=expected_role)
    return result


def validate_canonical_reports(value: Any) -> BerkshireCanonicalReports:
    if not isinstance(value, Mapping):
        raise ValueError("Berkshire Team Lead output must be an object")
    result: BerkshireCanonicalReports = {}
    for key in CANONICAL_REPORT_KEYS:
        report = value.get(key)
        if not isinstance(report, str) or not report.strip():
            raise ValueError(f"Team Lead canonical field {key!r} is empty")
        result[key] = report.strip()
    if len(set(result.values())) < 3:
        raise ValueError("Team Lead must map evidence into distinct canonical report contexts")
    assert_analysis_only(result, label="team_lead")
    return result


def parse_json_response(response: Any, *, label: str) -> Any:
    content = response.get("content") if isinstance(response, Mapping) else getattr(response, "content", response)
    if not isinstance(content, str):
        raise ValueError(f"{label} response is not text JSON")
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} response is not valid JSON") from exc
