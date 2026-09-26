"""Pure execution-policy checks shared by initial submission and recovery.

Price bounds apply to the current executable quote, not a guaranteed market
fill. Stop risk is a planned loss bound; gaps/slippage can exceed it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import math


def canonical_protective_price(value):
    """Return the exact cent precision used by the equity broker request."""
    price = round(float(value), 2)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("invalid protective price")
    return price


def utc_timestamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


@dataclass(frozen=True)
class RiskRewardTerms:
    worst_entry_price: float
    risk_per_share: float
    reward_per_share: float
    risk_fraction: float
    ratio: float


def worst_case_risk_reward(
    *, target_position, minimum_price, maximum_price, stop_loss_price,
    take_profit_price,
) -> RiskRewardTerms | None:
    """Calculate R/R at the least favorable price in the authorized range."""
    try:
        low, high = map(float, (minimum_price, maximum_price))
        stop = canonical_protective_price(stop_loss_price)
        target = canonical_protective_price(take_profit_price)
        if not all(math.isfinite(value) and value > 0 for value in (low, high, stop, target)):
            return None
        if low > high:
            return None
        direction = str(getattr(target_position, "value", target_position)).upper()
        if direction not in {"LONG", "SHORT"}:
            return None
        short = direction == "SHORT"
        if (short and (stop <= high or target >= low)) or (not short and (stop >= low or target <= high)):
            return None
        worst = low if short else high
        risk = stop - worst if short else worst - stop
        reward = worst - target if short else target - worst
        if risk <= 0 or reward <= 0:
            return None
        risk_fraction, ratio = risk / worst, reward / risk
        if not math.isfinite(risk_fraction) or not math.isfinite(ratio):
            return None
        return RiskRewardTerms(worst, risk, reward, risk_fraction, ratio)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_opening_intent(intent: dict) -> bool:
    transition = str(intent.get("position_transition") or "").upper()
    if transition in {"OPEN_LONG", "OPEN_SHORT", "REVERSE_TO_LONG", "REVERSE_TO_SHORT"}:
        return True
    for row in intent.get("planned_actions") or []:
        action = str(row.get("action") or "").lower() if isinstance(row, dict) else ""
        if action.startswith("open_"):
            return True
    target = str(intent.get("target_position") or "").upper()
    current = str(intent.get("current_position") or "").upper()
    return target in {"LONG", "SHORT"} and target != current


def entry_check(intent: dict, *, now=None, price=None, equity=None, requested=None) -> tuple[float | None, str | None]:
    """Return clipped notional or a reason to refuse new exposure."""
    now = now or datetime.now(timezone.utc)
    policy = intent.get("entry_policy") or {}
    controls = intent.get("risk_controls") or {}
    try:
        if policy.get("status") != "READY" or not str(policy.get("confirmation") or "").strip():
            raise ValueError("entry conditions not confirmed: WAIT")
        expires = utc_timestamp(policy.get("expires_at"))
        deadline = utc_timestamp(policy.get("exit_by"))
        generated = utc_timestamp(intent.get("generated_at"))
        if generated > now + timedelta(seconds=2):
            raise ValueError("decision timestamp is in the future")
        if now >= expires or expires <= generated:
            raise ValueError("entry authorization expired")
        if not expires < deadline <= generated + timedelta(days=30):
            raise ValueError("exit deadline must follow entry expiry and be within 30 calendar days")
        low = float(policy.get("minimum_price"))
        high = float(policy.get("maximum_price"))
        stop = canonical_protective_price(controls.get("stop_loss_price"))
        risk = float(policy.get("risk_fraction", 0.01))
        if not all(math.isfinite(v) and v > 0 for v in (low, high, stop, risk)) or low > high or risk > 0.03:
            raise ValueError("invalid entry prices, stop or risk budget")
        short = intent.get("target_position") == "SHORT"
        if (short and stop <= high) or (not short and stop >= low):
            raise ValueError("stop must be outside the entire entry range on the loss side")
        target = controls.get("take_profit_price")
        if _is_opening_intent(intent):
            if target is None:
                raise ValueError("opening exposure requires a numeric take-profit price for minimum 2:1 R/R")
            terms = worst_case_risk_reward(
                target_position=intent.get("target_position"),
                minimum_price=low,
                maximum_price=high,
                stop_loss_price=stop,
                take_profit_price=target,
            )
            if terms is None:
                raise ValueError("stop and target must define positive risk and reward beyond the entry range")
            if terms.ratio < 2.0:
                raise ValueError(f"opening exposure requires at least 2:1 R/R at worst authorized entry (got {terms.ratio:.2f}:1)")
        elif target is not None:
            target = canonical_protective_price(target)
            if not math.isfinite(target) or target <= 0 or (short and target >= low) or (not short and target <= high):
                raise ValueError("target must be beyond the entry range on the profit side")
        if "/" in intent.get("symbol", ""):
            raise ValueError("this execution contract requires broker-side equity protective orders")
        if price is None:
            return None, None
        if not math.isfinite(price) or not low <= price <= high:
            raise ValueError("current executable quote is outside the authorized entry range")
        equity, requested = float(equity), float(requested)
        if not all(math.isfinite(v) and v > 0 for v in (equity, requested)):
            raise ValueError("invalid account equity or requested amount")
        # Use the same worst authorized entry used by the deterministic R/R gate.
        distance_fraction = terms.risk_fraction if _is_opening_intent(intent) else (
            (stop - low) / low if short else (high - stop) / high
        )
        amount = min(requested, equity * risk / distance_fraction)
        cap = policy.get("maximum_notional")
        if cap is not None:
            cap = float(cap)
            if not math.isfinite(cap) or cap <= 0:
                raise ValueError("invalid maximum notional")
            amount = min(amount, cap)
        return math.floor(amount * 100) / 100, None
    except (TypeError, ValueError, OverflowError) as exc:
        return None, f"Execution policy: {exc}"
