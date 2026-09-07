"""Pure execution-policy checks shared by initial submission and recovery.

Price bounds apply to the current executable quote, not a guaranteed market
fill. Stop risk is a planned loss bound; gaps/slippage can exceed it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math


def utc_timestamp(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


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
        stop = float(controls.get("stop_loss_price"))
        risk = float(policy.get("risk_fraction", 0.01))
        if not all(math.isfinite(v) and v > 0 for v in (low, high, stop, risk)) or low > high or risk > 0.03:
            raise ValueError("invalid entry prices, stop or risk budget")
        short = intent.get("target_position") == "SHORT"
        if (short and stop <= high) or (not short and stop >= low):
            raise ValueError("stop must be outside the entire entry range on the loss side")
        target = controls.get("take_profit_price")
        if target is not None:
            target = float(target)
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
        # Use the worst price within the authorized range, not an unrelated ATR.
        distance_fraction = (stop - low) / low if short else (high - stop) / high
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
