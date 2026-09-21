"""Coordinator for the true four-role Berkshire analysis topology."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from typing import Any, Mapping

from langchain_core.messages import AIMessage

from tradingagents.experiments.evidence_snapshot import load_evidence_packet

from .calculations import cross_validate, verify_market_cap, verify_valuation
from .roles import ROLE_NAMES, run_role, team_lead_messages
from .schemas import (
    CANONICAL_REPORT_KEYS,
    parse_json_response,
    validate_canonical_reports,
)


class BerkshireAnalysisError(RuntimeError):
    """Any role/schema/provider error fails the entire backend closed.

    ``retryable`` preserves transient provider failures across the Berkshire
    team boundary.  Semantic/schema failures remain terminal.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        self.retryable = bool(retryable)
        super().__init__(message)


def _is_retryable_failure(exc: BaseException) -> bool:
    if isinstance(exc, OSError) or type(exc).__name__ in {
        "TimeoutError",
        "ConnectionError",
        "BrokenPipeError",
        "ConnectionResetError",
    }:
        return True
    return (
        getattr(exc, "category", None) == "transient"
        or getattr(exc, "retryable", False) is True
    )


def _calculations(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Extract only explicitly supplied numeric values; never invent inputs."""

    values: dict[str, Any] = {}
    fundamentals = evidence.get("fundamentals", {})
    if isinstance(fundamentals, Mapping):
        values.update(fundamentals.get("calculations", {}) or {})
    result: dict[str, Any] = {}
    try:
        if {"price", "shares_outstanding"} <= values.keys():
            result["market_cap"] = verify_market_cap(
                price=values["price"],
                shares_outstanding=values["shares_outstanding"],
                reported_market_cap=values.get("reported_market_cap"),
            )
        if {"primary_value", "secondary_value"} <= values.keys():
            result["cross_validation"] = cross_validate(
                primary=values["primary_value"], secondary=values["secondary_value"]
            )
        if {"enterprise_value", "revenue"} <= values.keys():
            result["valuation"] = verify_valuation(
                enterprise_value=values["enterprise_value"],
                revenue=values["revenue"],
                earnings=values.get("earnings"),
            )
    except ValueError as exc:
        result["calculation_error"] = str(exc)
    return result


def _packet_from_state(state: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    packet = state.get("analysis_evidence")
    if isinstance(packet, Mapping) and packet.get("sha256"):
        return dict(packet)
    path = config.get("evidence_packet_path")
    if not path:
        raise BerkshireAnalysisError("Berkshire backend requires a frozen evidence packet")
    try:
        return load_evidence_packet(
            path,
            symbol=str(state.get("company_of_interest", "")),
            trade_date=str(state.get("trade_date", "")),
            expected_sha256=config.get("evidence_packet_sha256"),
        )
    except Exception as exc:
        raise BerkshireAnalysisError(str(exc)) from exc


def _run_team(llm: Any, state: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _packet_from_state(state, config)
    calculations = _calculations(evidence)
    role_reports: dict[str, Any] = {}
    failures: list[str] = []
    failure_exceptions: list[BaseException] = []

    with ThreadPoolExecutor(max_workers=len(ROLE_NAMES), thread_name_prefix="berkshire-role") as pool:
        futures = {
            pool.submit(run_role, llm, role, evidence, calculations): role
            for role in ROLE_NAMES
        }
        for future in as_completed(futures):
            role = futures[future]
            try:
                role_reports[role] = future.result()
            except Exception as exc:
                failure_exceptions.append(exc)
                failures.append(f"{role}: {type(exc).__name__}: {exc}")
    if failures or set(role_reports) != set(ROLE_NAMES):
        raise BerkshireAnalysisError(
            "Berkshire analysis failed closed; incomplete role team: " + "; ".join(sorted(failures)),
            retryable=any(_is_retryable_failure(exc) for exc in failure_exceptions),
        )

    try:
        lead_response = llm.invoke(team_lead_messages(role_reports, evidence))
        canonical = validate_canonical_reports(
            parse_json_response(lead_response, label="team_lead")
        )
    except Exception as exc:
        raise BerkshireAnalysisError(
            f"team_lead synthesis failed: {exc}",
            retryable=_is_retryable_failure(exc),
        ) from exc

    return {
        **canonical,
        "analysis_backend": "berkshire",
        "analysis_backend_meta": {
            "backend": "berkshire",
            "evidence_sha256": evidence["sha256"],
            "business_analyst": role_reports["business_analyst"],
            "financial_analyst": role_reports["financial_analyst"],
            "industry_researcher": role_reports["industry_researcher"],
            "risk_assessor": role_reports["risk_assessor"],
            "team_lead": canonical,
        },
        "analysis_evidence": evidence,
        # The shared Build Report Context node validates coverage using the
        # native downstream report names.  Berkshire's four upstream roles
        # have already been synthesized into all five canonical reports, so
        # expose both identities without pretending they were native analyst
        # calls.
        "analysis_status": {
            **{role: "completed" for role in ROLE_NAMES},
            "market": "completed",
            "social": "completed",
            "news": "completed",
            "fundamentals": "completed",
            "macro": "completed",
        },
        "analysis_errors": {},
        "messages": [AIMessage(content=json.dumps(canonical, ensure_ascii=False))],
    }


def create_berkshire_analysis_team(llm: Any, config: Mapping[str, Any] | None = None):
    """Return the graph node representing the Berkshire analysis team."""

    frozen_config = dict(config or {})

    def node(state):
        return _run_team(llm, state, frozen_config)

    return node
