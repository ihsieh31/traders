"""Build and verify one frozen evidence packet for an A/B pair.

The collector deliberately calls the same Toolkit methods used by the native
analysts.  Once persisted, analysis agents read only this file in frozen mode;
they never receive a live tool list.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from tradingagents.agents.utils.agent_utils import Toolkit
from tradingagents.long_run_support.state import atomic_write_json


class EvidenceIntegrityError(RuntimeError):
    """Raised when an evidence packet is missing, malformed, or tampered with."""


_SECTIONS = ("market", "fundamentals", "news", "macro", "social")
_MARKET_TZ = ZoneInfo("America/New_York")


def _market_today_iso() -> str:
    """US-market calendar date, independent of the operator host timezone."""

    return datetime.now(_MARKET_TZ).date().isoformat()


def _canonical_without_hash(packet: Mapping[str, Any]) -> str:
    body = {key: value for key, value in packet.items() if key != "sha256"}
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def evidence_packet_sha256(packet: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_without_hash(packet).encode("utf-8")).hexdigest()


def _invoke(tool: Any, args: Mapping[str, Any]) -> Any:
    if hasattr(tool, "invoke"):
        return tool.invoke(dict(args))
    if hasattr(tool, "run"):
        return tool.run(**dict(args))
    if callable(tool):
        return tool(**dict(args))
    raise TypeError(f"evidence source is not callable: {tool!r}")


def classify_source_value(value: Any) -> str:
    """Classify a captured adapter response before it becomes frozen evidence."""
    if value is None or value == "" or value == [] or value == {}:
        return "empty"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered.startswith("{"):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, Mapping):
                return classify_source_value(decoded)
        if lowered.startswith(("no indicator data available", "historical data unavailable",
                               "unavailable:", "no historical data available")):
            return "unavailable"
        if lowered.startswith(("error:", "error getting", "exception:", "timeout:",
                               "**error**")):
            return "error"
        # Macro/FRED adapters build one markdown report and append a
        # "### <indicator>\n**Error**: ..." line per failed series, so the
        # marker lands mid-document rather than at the start. Only a line that
        # BEGINS with the bolded marker counts, which keeps a bolded word
        # inside ordinary prose classified as real content.
        if any(line.strip().lower().startswith("**error**")
               for line in value.splitlines()[1:]):
            return "error"
        if lowered in {"error", "unavailable", "timed out", "timeout"}:
            return "error" if lowered != "unavailable" else "unavailable"
    if isinstance(value, Mapping):
        status = str(value.get("status") or "").lower()
        if "error" in value or status in {"error", "failed", "failure"}:
            return "error"
        if status == "unavailable":
            return "unavailable"
    return "available"


def _usable_source_value(value: Any) -> bool:
    return classify_source_value(value) == "available"


def _available(toolkit: Any, capability: str | None) -> bool:
    if not capability:
        return True
    probe = getattr(toolkit, capability, None)
    if not callable(probe):
        return True
    try:
        return bool(probe())
    except Exception:
        return False


def _capture_source(
    toolkit: Any,
    *,
    section: dict[str, Any],
    section_name: str,
    source_name: str,
    tool_name: str,
    args: Mapping[str, Any],
    capability: str | None,
    live_only: bool,
    trade_date: str,
    sources: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    historical = trade_date < _market_today_iso()
    if live_only and historical:
        section[source_name] = {
            "status": "unavailable",
            "reason": "live-only source has no verified point-in-time cutoff",
            "as_of": trade_date,
        }
        sources.append(
            {
                "section": section_name,
                "source": source_name,
                "status": "unavailable",
                "as_of": trade_date,
            }
        )
        return
    if not _available(toolkit, capability):
        section[source_name] = {
            "status": "unavailable",
            "reason": "source is not configured or unavailable",
            "as_of": trade_date,
        }
        sources.append(
            {
                "section": section_name,
                "source": source_name,
                "status": "unavailable",
                "as_of": trade_date,
            }
        )
        return

    tool = getattr(toolkit, tool_name, None)
    if tool is None:
        section[source_name] = {
            "status": "unavailable",
            "reason": f"Toolkit method {tool_name} is missing",
            "as_of": trade_date,
        }
        errors.append(
            {
                "section": section_name,
                "source": source_name,
                "error_type": "MissingToolkitMethod",
                "error": tool_name,
            }
        )
        return
    try:
        value = _invoke(tool, args)
        status = classify_source_value(value)
        section[source_name] = {
            "status": status,
            "as_of": trade_date,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "value": value,
        }
        sources.append(
            {
                "section": section_name,
                "source": source_name,
            "status": section[source_name]["status"],
                "as_of": trade_date,
            }
        )
    except Exception as exc:
        section[source_name] = {
            "status": "error",
            "as_of": trade_date,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        errors.append(
            {
                "section": section_name,
                "source": source_name,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )


def _capture_packet(toolkit: Any, symbol: str, trade_date: str) -> dict[str, Any]:
    if trade_date > _market_today_iso():
        raise EvidenceIntegrityError(
            f"trade_date {trade_date} is in the future; evidence cannot be point-in-time safe"
        )
    sections = {name: {} for name in _SECTIONS}
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    common = {"ticker": symbol, "curr_date": trade_date}

    # These are the concrete source methods used by the five native analyst
    # factories.  The collector stores each response separately so later audit
    # can distinguish missing data from a synthesized analyst report.
    alpaca_market = bool(
        getattr(toolkit, "config", {}).get("online_tools", True)
        and _available(toolkit, "has_alpaca_credentials")
    )
    if alpaca_market:
        market_calls = [
            ("technical_brief", "get_technical_brief", {"symbol": symbol, "curr_date": trade_date}, None, False),
            ("alpaca_ohlcv", "get_alpaca_data_report", {"symbol": symbol, "curr_date": trade_date, "look_back_days": 90, "timeframe": "1Day"}, "has_alpaca_credentials", False),
            ("indicator_rsi", "get_stockstats_indicators_report_online", {"symbol": symbol, "indicator": "rsi_14", "curr_date": trade_date, "look_back_days": 90, "timeframe": "1Day", "max_points": 90}, "has_alpaca_credentials", False),
            ("indicator_macd", "get_stockstats_indicators_report_online", {"symbol": symbol, "indicator": "macd", "curr_date": trade_date, "look_back_days": 90, "timeframe": "1Day", "max_points": 90}, "has_alpaca_credentials", False),
            ("indicator_trend", "get_stockstats_indicators_report_online", {"symbol": symbol, "indicator": "close_21_ema", "curr_date": trade_date, "look_back_days": 90, "timeframe": "1Day", "max_points": 90}, "has_alpaca_credentials", False),
        ]
    else:
        market_calls = [
            ("indicator_rsi", "get_stockstats_indicators_report", {"symbol": symbol, "indicator": "rsi_14", "curr_date": trade_date, "look_back_days": 90}, None, False),
            ("indicator_macd", "get_stockstats_indicators_report", {"symbol": symbol, "indicator": "macd", "curr_date": trade_date, "look_back_days": 90}, None, False),
            ("indicator_trend", "get_stockstats_indicators_report", {"symbol": symbol, "indicator": "close_21_ema", "curr_date": trade_date, "look_back_days": 90}, None, False),
        ]
    for name, method, args, capability, live_only in market_calls:
        _capture_source(
            toolkit, section=sections["market"], section_name="market",
            source_name=name, tool_name=method, args=args,
            capability=capability, live_only=live_only, trade_date=trade_date,
            sources=sources, errors=errors,
        )

    fundamental_calls = [
        ("sec_ir", "get_sec_ir_source", common, None, False),
        ("finnhub_insider_sentiment", "get_finnhub_company_insider_sentiment", common, "has_finnhub", False),
        ("finnhub_insider_transactions", "get_finnhub_company_insider_transactions", common, "has_finnhub", False),
        ("simfin_balance_sheet", "get_simfin_balance_sheet", common, "has_simfin_data", False),
        ("simfin_cashflow", "get_simfin_cashflow", common, "has_simfin_data", False),
        ("simfin_income_stmt", "get_simfin_income_stmt", common, "has_simfin_data", False),
        ("fundamentals_web_search", "get_fundamentals_openai", common, "has_openai_web_search", True),
    ]
    for name, method, args, capability, live_only in fundamental_calls:
        _capture_source(
            toolkit, section=sections["fundamentals"], section_name="fundamentals",
            source_name=name, tool_name=method, args=args,
            capability=capability, live_only=live_only, trade_date=trade_date,
            sources=sources, errors=errors,
        )

    news_calls = [
        ("finnhub_recent", "get_finnhub_news_recent", {"ticker": symbol, "curr_date": trade_date, "look_back_days": 7}, "has_finnhub", False),
        ("google_news", "get_google_news", {"query": symbol, "curr_date": trade_date}, None, True),
        ("global_news", "get_global_news_openai", {"curr_date": trade_date, "ticker_context": symbol}, "has_openai_web_search", True),
        ("coindesk_news", "get_coindesk_news", {"ticker": symbol, "curr_date": trade_date}, "has_coindesk", True),
    ]
    for name, method, args, capability, live_only in news_calls:
        _capture_source(
            toolkit, section=sections["news"], section_name="news",
            source_name=name, tool_name=method, args=args,
            capability=capability, live_only=live_only, trade_date=trade_date,
            sources=sources, errors=errors,
        )

    macro_calls = [
        ("macro_analysis", "get_macro_analysis", {"curr_date": trade_date}, "has_fred", False),
        ("economic_indicators", "get_economic_indicators", {"curr_date": trade_date}, "has_fred", False),
        ("yield_curve", "get_yield_curve_analysis", {"curr_date": trade_date}, "has_fred", False),
        ("macro_news", "get_macro_news_openai", {"curr_date": trade_date, "ticker_context": symbol}, "has_openai_web_search", True),
    ]
    for name, method, args, capability, live_only in macro_calls:
        _capture_source(
            toolkit, section=sections["macro"], section_name="macro",
            source_name=name, tool_name=method, args=args,
            capability=capability, live_only=live_only, trade_date=trade_date,
            sources=sources, errors=errors,
        )

    social_calls = [
        ("social_web_search", "get_stock_news_openai", {"ticker": symbol, "curr_date": trade_date}, "has_openai_web_search", True),
        ("reddit_stock", "get_reddit_stock_info", {"ticker": symbol, "curr_date": trade_date}, None, True),
        ("reddit_global", "get_reddit_news", {"curr_date": trade_date}, None, True),
    ]
    for name, method, args, capability, live_only in social_calls:
        _capture_source(
            toolkit, section=sections["social"], section_name="social",
            source_name=name, tool_name=method, args=args,
            capability=capability, live_only=live_only, trade_date=trade_date,
            sources=sources, errors=errors,
        )

    packet: dict[str, Any] = {
        "schema_version": 1,
        "symbol": symbol,
        "trade_date": trade_date,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **sections,
        "sources": sources,
        "errors": errors,
    }
    packet["sha256"] = evidence_packet_sha256(packet)
    return packet


def _validate_packet(packet: Mapping[str, Any], *, symbol: str, trade_date: str) -> dict[str, Any]:
    if not isinstance(packet, Mapping):
        raise EvidenceIntegrityError("evidence packet must be a JSON object")
    if packet.get("schema_version") != 1:
        raise EvidenceIntegrityError("unsupported evidence packet schema_version")
    if packet.get("symbol") != symbol or packet.get("trade_date") != trade_date:
        raise EvidenceIntegrityError(
            "evidence identity mismatch: packet must match symbol and trade_date"
        )
    if trade_date > _market_today_iso():
        raise EvidenceIntegrityError(
            f"trade_date {trade_date} is in the future; evidence cannot be point-in-time safe"
        )
    for section in _SECTIONS:
        if not isinstance(packet.get(section), Mapping):
            raise EvidenceIntegrityError(f"evidence section {section!r} is missing")
    expected = packet.get("sha256")
    actual = evidence_packet_sha256(packet)
    if not isinstance(expected, str) or expected != actual:
        raise EvidenceIntegrityError(
            f"evidence hash mismatch: expected {expected!r}, calculated {actual!r}"
        )
    return deepcopy(dict(packet))


def validate_evidence_completeness(
    packet: Mapping[str, Any], *, sections: tuple[str, ...] = _SECTIONS
) -> None:
    """Require at least one usable captured source in every requested section."""

    missing: list[str] = []
    for section_name in sections:
        section = packet.get(section_name)
        usable = isinstance(section, Mapping) and any(
            isinstance(entry, Mapping)
            and entry.get("status") == "available"
            and _usable_source_value(entry.get("value"))
            for entry in section.values()
        )
        if not usable:
            missing.append(section_name)
    if missing:
        raise EvidenceIntegrityError(
            "evidence completeness gate failed; no usable source for: "
            + ", ".join(missing)
        )


def load_evidence_packet(
    path: str | Path,
    *,
    symbol: str,
    trade_date: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    packet_path = Path(path)
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceIntegrityError(f"cannot read evidence packet {packet_path}") from exc
    validated = _validate_packet(packet, symbol=symbol, trade_date=trade_date)
    validate_evidence_completeness(validated)
    if expected_sha256 and validated.get("sha256") != expected_sha256:
        raise EvidenceIntegrityError(
            "evidence packet does not match the SHA-256 pinned by the run configuration"
        )
    return validated


def build_or_load_evidence_packet(
    path: str | Path,
    *,
    symbol: str,
    trade_date: str,
    toolkit: Any | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create exactly one packet, or load and verify the existing packet."""

    packet_path = Path(path)
    if packet_path.exists():
        packet = load_evidence_packet(
            packet_path,
            symbol=symbol,
            trade_date=trade_date,
            expected_sha256=(config or {}).get("evidence_packet_sha256"),
        )
    else:
        packet = _capture_packet(toolkit or Toolkit(config=dict(config or {})), symbol, trade_date)
        validate_evidence_completeness(packet)
        configured_hash = (config or {}).get("evidence_packet_sha256")
        if configured_hash and configured_hash != packet["sha256"]:
            raise EvidenceIntegrityError("configured evidence_packet_sha256 does not match captured packet")
        atomic_write_json(packet_path, packet)

    configured_hash = (config or {}).get("evidence_packet_sha256")
    if configured_hash and configured_hash != packet["sha256"]:
        raise EvidenceIntegrityError(
            "configured evidence_packet_sha256 does not match the packet on disk"
        )
    return packet
