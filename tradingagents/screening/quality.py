"""Evidence for screening inputs. Unknown business quality is never a bad company."""

from collections import Counter
from datetime import date
import hashlib
import json
import math
import re

import numpy as np

QUALITY_VERSION = 1


def identity_issues(universe):
    """Reject ambiguous instrument snapshots, including every conflicting row."""
    symbols = Counter(r.get("symbol") for r in universe if isinstance(r, dict) and isinstance(r.get("symbol"), str))
    ids = Counter(r.get("asset_id") for r in universe if isinstance(r, dict) and isinstance(r.get("asset_id"), str) and r["asset_id"])
    issues = []
    for row in universe:
        if not isinstance(row, dict):
            issues.append("invalid_identity")
            continue
        symbol, asset_id = row.get("symbol"), row.get("asset_id")
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", symbol):
            reason = "invalid_identity"
        elif symbols[symbol] > 1 or (isinstance(asset_id, str) and ids[asset_id] > 1):
            reason = "ambiguous_identity"
        elif not isinstance(asset_id, str) or not asset_id.strip() or not isinstance(row.get("identity_source"), str) or not row["identity_source"].strip():
            reason = "missing_identity_evidence"
        elif row.get("asset_class") != "us_equity" or row.get("asset_status") != "active" or row.get("tradable") is not True:
            reason = "unsupported_instrument"
        else:
            reason = None
        issues.append(reason)
    return issues


def reason_category(reason):
    if reason is None:
        return "usable"
    if reason in {"missing_identity_evidence", "missing_market_cap", "missing_market_cap_source", "missing_bars", "insufficient_bars", "stale_last_bar", "missing_session", "missing_bar_source"}:
        return "data_gap"
    if reason.startswith("below_min_") or reason == "zero_volume_baseline":
        return "eligibility"
    if reason.startswith("exclusion_"):
        return "risk_policy"
    return "data_error"


def quality_record(entry):
    entry = entry if isinstance(entry, dict) else {}
    keys = ("symbol", "asset_id", "identity_source", "asset_class", "asset_status", "tradable", "market_cap_source")
    record = {k: entry.get(k) if isinstance(entry.get(k), (str, bool)) else None for k in keys}
    record.update(status="usable", reason=None, category="usable", market_cap=None)
    return record


def bar_evidence(window, frame, *, as_of, adjustment):
    values = np.column_stack([window[k].to_numpy(dtype=np.float64)
                              for k in ("open", "high", "low", "close", "volume")]).tolist()
    rows = [[stamp.isoformat(), *row] for stamp, row in zip(window["timestamp"], values)]
    return {"as_of": str(as_of), "source": frame.attrs.get("source"), "feed": frame.attrs.get("feed"),
            "adjustment": frame.attrs.get("adjustment"), "count": len(window),
            "sha256": hashlib.sha256(json.dumps(rows,separators=(",", ":")).encode()).hexdigest(), "window": rows,
            "last_close": float(window["close"].iloc[-1])}


def build_quality_report(stats, *, as_of, observed_at, adjustment):
    return {"version": QUALITY_VERSION, "scope": "current_instrument_snapshot", "as_of": str(as_of),
            "observed_at": observed_at, "feed": "sip", "adjustment": adjustment,
            "financials": {"status": "unknown", "reason": "not_loaded_or_scope_verified", "used_for_screening": False},
            "business_quality": "unknown", "historical_strategy_validation": "not_assessed",
            "counts": dict(Counter(r["category"] for r in stats.records)), "records": stats.records}


def quality_report_valid(payload, config):
    """Same read-side checks for daily caches, frozen A/B and risk context."""
    from .metrics import EligibilityThresholds, FACTOR_KEYS, compute_features

    try:
        report = payload["data_quality"]
        if (type(report["version"]) is not int or report["version"] != QUALITY_VERSION or report["scope"] != "current_instrument_snapshot"
                or report["as_of"] != payload["as_of"] or report["observed_at"] != payload["generated_at"]
                or report["feed"] != "sip" or report["adjustment"] != payload["adjustment_policy"]
                or report["adjustment"] != config.get("screening_bar_adjustment", "split")
                or report["financials"] != {"status": "unknown", "reason": "not_loaded_or_scope_verified", "used_for_screening": False}
                or report["business_quality"] != "unknown" or report["historical_strategy_validation"] != "not_assessed"):
            return False
        date.fromisoformat(report["as_of"])
        records = report["records"]
        if not isinstance(records, list) or len(records) != payload["stats"]["universe_total"]:
            return False
        if dict(Counter(r["category"] for r in records)) != report["counts"]:
            return False
        if dict(Counter(r["reason"] for r in records if r["status"] == "excluded")) != payload["stats"]["excluded"]:
            return False
        good = [r for r in records if r["status"] == "usable"]
        if any(identity_issues(good)):
            return False
        for r in records:
            if (r["category"] != reason_category(r["reason"]) or
                    r["status"] != ("usable" if r["reason"] is None else "excluded")):
                return False
        thresholds = EligibilityThresholds.from_config(config)
        by_symbol = {r["symbol"]: r for r in good}
        for row in payload["top40"]:
            evidence = by_symbol[row["symbol"]]
            cap, bars = evidence["market_cap"], evidence["bars"]
            if (isinstance(cap, bool) or not isinstance(cap, (float, int)) or not math.isfinite(cap)
                    or cap < thresholds.min_market_cap_usd or not evidence["market_cap_source"]
                    or bars["as_of"] != report["as_of"] or bars["feed"] != "sip" or not bars["source"]
                    or bars["adjustment"] != report["adjustment"] or bars["count"] != thresholds.required_bars
                    or not re.fullmatch(r"[0-9a-f]{64}", bars["sha256"])):
                return False
            factors = evidence["factors"]
            if set(factors) != set(("price",)+FACTOR_KEYS):
                return False
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in factors.values()):
                return False
            if (any(row.get(k) != v for k, v in factors.items()) or bars["last_close"] != factors["price"]
                    or factors["price"] < thresholds.min_price or factors["adv20"] < thresholds.min_adv20_usd):
                return False
        # Retain and replay raw prices only for the <=20 selected names.
        import pandas as pd
        for row in payload["top20"]:
            evidence = by_symbol[row["symbol"]]
            bars = evidence["bars"]
            raw = bars["window"]
            if (not isinstance(raw,list) or len(raw) != thresholds.required_bars
                    or any(not isinstance(r,list) or len(r)!=6 or not isinstance(r[0],str)
                           or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in r[1:]) for r in raw)
                    or hashlib.sha256(json.dumps(raw,separators=(",", ":")).encode()).hexdigest() != bars["sha256"]):
                return False
            frame = pd.DataFrame(raw,columns=["timestamp","open","high","low","close","volume"])
            frame["timestamp"] = pd.to_datetime(frame["timestamp"],utc=True,errors="coerce")
            if frame["timestamp"].isna().any() or not frame["timestamp"].is_monotonic_increasing or frame["timestamp"].duplicated().any():
                return False
            sessions = frame["timestamp"].dt.tz_convert("US/Eastern").dt.date
            if sessions.duplicated().any() or str(sessions.iloc[-1]) != report["as_of"]:
                return False
            values = frame[["open","high","low","close","volume"]]
            if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in values.to_numpy().flat):
                return False
            if ((frame[["open","high","low","close"]]<=0).any().any() or (frame["volume"]<0).any()
                    or (frame["low"]>frame[["open","close"]].min(axis=1)).any()
                    or (frame["high"]<frame[["open","close"]].max(axis=1)).any()):
                return False
            computed, reason = compute_features(row["symbol"],frame,thresholds=thresholds)
            if reason or any(not math.isclose(v,evidence["factors"][k],rel_tol=1e-12,abs_tol=1e-12) for k,v in computed.factor_row().items()):
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def assess_financial_facts(facts, *, as_of=None):
    """A ratio may be computed only after values AND economic scope are proven."""
    # Canonical concepts, not guessed XBRL tag aliases. Source tags are retained
    # by the caller; a reviewed ProfitLoss mapping need not be misnamed as a tag.
    keys = ("net_income", "operating_cash_flow", "assets")
    if not isinstance(facts, dict) or any(not isinstance(facts.get(k), dict) for k in keys):
        return {"status": "unknown", "reason": "missing_required_facts"}
    rows = [facts[k] for k in keys]
    try:
        if as_of is None or any(not isinstance(r.get("available_at"), str) for r in rows):
            return {"status": "unknown", "reason": "availability_not_verified"}
        signal = date.fromisoformat(str(as_of))
        if any(date.fromisoformat(r["available_at"][:10]) >= signal for r in rows):
            return {"status": "invalid", "reason": "not_available_before_signal"}
        for r in rows:
            if isinstance(r.get("value"), bool) or not math.isfinite(float(r["value"])):
                return {"status": "invalid", "reason": "invalid_numeric_fact"}
        if float(rows[2]["value"]) <= 0:
            return {"status": "invalid", "reason": "nonpositive_assets"}
        if any(not r.get("source") or not r.get("unit") or not r.get("period_end") or not r.get("scope") for r in rows):
            return {"status": "unknown", "reason": "missing_period_unit_scope_or_source"}
        if {r["unit"] for r in rows} != {"USD"}:
            return {"status": "invalid", "reason": "unit_mismatch"}
        if len({r["scope"] for r in rows}) != 1:
            return {"status": "invalid", "reason": "scope_mismatch"}
        if rows[0]["scope"] != "consolidated":
            return {"status": "unknown", "reason": "unsupported_scope"}
        ends = {date.fromisoformat(r["period_end"]) for r in rows}
        if len(ends) != 1 or max(ends) >= signal or [r.get("qtrs") for r in rows] != [4, 4, 0]:
            return {"status": "invalid", "reason": "period_mismatch"}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"status": "unknown", "reason": "malformed_fact_evidence"}
    return {"status": "usable", "reason": None}


def historical_research_gate(evidence, *, financials=False, shorts=False):
    if not isinstance(evidence,dict):
        evidence = {}
    required = ["historical_instrument_identity", "delisting_outcomes", "price_adjustments_and_dividends", "point_in_time_availability"]
    if financials:
        required.append("financial_period_unit_scope")
    if shorts:
        required.append("historical_borrow_availability_and_costs")
    missing = [k for k in required if evidence.get(k) != "verified"]
    return {"status": "STOPPED" if missing else "READY", "unverified_requirements": missing,
            "meaning": "input readiness only, not profitable strategy validation"}
