"""Role prompts and invocation helpers for the Berkshire research team."""

from __future__ import annotations

import json
from typing import Any, Mapping

from langchain_core.messages import HumanMessage, SystemMessage

from tradingagents.prompts import load_prompt

from .schemas import ROLE_NAMES, parse_json_response, validate_role_report


_ROLE_BRIEFS = {
    "business_analyst": "Business model, revenue structure, customer value, repeat purchase, switching costs, pricing power, network effects, scale advantage, moat durability, and management quality.",
    "financial_analyst": "Revenue, earnings, free cash flow, margins, cash, debt, capital allocation, ROIC, valuation assumptions, data inconsistencies, and explicit missing numbers. Use supplied deterministic calculations only.",
    "industry_researcher": "Industry structure, competition, market share, substitutes, supplier/customer power, TAM, industry cycle, technology change, regulation, macro sensitivity, and historical analogues.",
    "risk_assessor": "Inversion: why the thesis could be wrong, permanent business destruction, management failures, regulation, disruption, accounting/data uncertainty, valuation sensitivity, unknowns, and missing evidence. Do not summarize the other roles.",
}


def _invoke(llm: Any, messages: list[Any]) -> Any:
    return llm.invoke(messages)


def role_messages(role: str, evidence: Mapping[str, Any], calculations: Mapping[str, Any] | None = None) -> list[Any]:
    template_name = {
        "business_analyst": "berkshire_team/business_analyst",
        "financial_analyst": "berkshire_team/financial_analyst",
        "industry_researcher": "berkshire_team/industry_researcher",
        "risk_assessor": "berkshire_team/risk_assessor",
    }[role]
    system = load_prompt(template_name)
    human = (
        f"Role name: {role}\n"
        "FROZEN EVIDENCE PACKET (the only allowed source):\n"
        f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)}\n\n"
        f"Role focus: {_ROLE_BRIEFS[role]}\n"
        f"Deterministic calculations available: {json.dumps(calculations or {}, ensure_ascii=False, default=str)}\n\n"
        "Return JSON only with keys: role, thesis, key_facts, uncertainties, risks, valuation_notes, evidence_refs."
    )
    return [SystemMessage(content=system), HumanMessage(content=human)]


def run_role(llm: Any, role: str, evidence: Mapping[str, Any], calculations: Mapping[str, Any] | None = None):
    response = _invoke(llm, role_messages(role, evidence, calculations))
    return validate_role_report(parse_json_response(response, label=role), expected_role=role)


def team_lead_messages(role_reports: Mapping[str, Any], evidence: Mapping[str, Any]) -> list[Any]:
    system = load_prompt("berkshire_team/team_lead")
    human = (
        "FROZEN EVIDENCE PACKET:\n"
        f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)}\n\n"
        "FOUR COMPLETE ROLE REPORTS:\n"
        f"{json.dumps(role_reports, ensure_ascii=False, sort_keys=True, default=str)}\n\n"
        "Return JSON only with exactly these non-empty string keys: "
        "market_report, sentiment_report, news_report, fundamentals_report, macro_report."
    )
    return [SystemMessage(content=system), HumanMessage(content=human)]
