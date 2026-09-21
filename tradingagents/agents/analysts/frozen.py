"""Shared frozen-evidence path for the native analyst factories."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from tradingagents.experiments.evidence_snapshot import (
    EvidenceIntegrityError,
    load_evidence_packet,
)
from tradingagents.prompt_capture import capture_agent_prompt


_ROLE_GUIDANCE = {
    "market": "Analyze OHLCV, technical brief, indicators, price/date metadata, and market structure.",
    "social": "Analyze only supplied social and sentiment evidence. If it is absent, say insufficient sentiment evidence.",
    "news": "Analyze only supplied company, sector, and macro news with publication/retrieval timestamps.",
    "fundamentals": "Analyze business, financial statements, cash flow, margins, capital allocation, and valuation inputs.",
    "macro": "Analyze only supplied macro indicators, macro interpretation, rates, cycles, and structural trends.",
}


def _content(response: Any) -> str:
    if isinstance(response, dict):
        value = response.get("content") or response.get("text") or response
        return str(value).strip()
    return str(getattr(response, "content", response) or "").strip()


def create_frozen_analyst(llm: Any, toolkit: Any, analyst: str, report_key: str):
    """Create a no-tool analyst node backed by one verified EvidencePacket."""

    def frozen_node(state):
        symbol = str(state.get("company_of_interest", ""))
        trade_date = str(state.get("trade_date", ""))
        packet = state.get("analysis_evidence")
        if not packet:
            path = toolkit.config.get("evidence_packet_path")
            if not path:
                raise EvidenceIntegrityError("frozen analysis requires evidence_packet_path")
            packet = load_evidence_packet(
                path,
                symbol=symbol,
                trade_date=trade_date,
                expected_sha256=toolkit.config.get("evidence_packet_sha256"),
            )

        section = packet[{"market": "market", "social": "social", "news": "news", "fundamentals": "fundamentals", "macro": "macro"}[analyst]]
        system = (
            "You are a research-only Traders analyst operating in frozen-evidence mode.\n"
            "Use only the supplied EvidencePacket. Do not call tools, browse, or infer unavailable facts.\n"
            "Do not issue BUY, SELL, LONG, SHORT, HOLD, position sizing, portfolio weights, executable orders, or final trading instructions.\n"
            f"Role guidance: {_ROLE_GUIDANCE[analyst]}\n"
            "Preserve missing, conflicting, and stale evidence explicitly."
        )
        human = (
            f"Symbol: {symbol}\nAs-of date: {trade_date}\n"
            "FROZEN EVIDENCE PACKET SECTION:\n"
            f"{json.dumps(section, ensure_ascii=False, sort_keys=True, default=str)}\n\n"
            "Write an evidence-grounded research report with verified observations, implications, "
            "uncertainties, and missing/conflicting evidence."
        )
        capture_agent_prompt(report_key, system + "\n\n" + human, symbol)
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        content = _content(response)
        if not content:
            return {
                "messages": [AIMessage(content="")],
                report_key: "",
                "analysis_status": {**(state.get("analysis_status") or {}), analyst: "failed"},
                "analysis_errors": {**(state.get("analysis_errors") or {}), analyst: f"{analyst} analyst returned an empty frozen-evidence report"},
            }
        return {
            "messages": [AIMessage(content=content)],
            report_key: content,
            "analysis_status": {**(state.get("analysis_status") or {}), analyst: "completed"},
            "analysis_errors": state.get("analysis_errors") or {},
        }

    return frozen_node
