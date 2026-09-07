"""Structured output schemas for decision agents.

Executable Alpaca actions remain BUY/HOLD/SELL or LONG/NEUTRAL/SHORT.
The upstream 5-tier rating is advisory metadata only.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Literal

from pydantic import BaseModel, Field

_PRICE_PATTERN = re.compile(r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")


def extract_protective_price(guidance: Optional[str]) -> Optional[float]:
    """Extract the first absolute price level from free-text risk guidance.

    Returns None for qualitative guidance ("below support"), relative values
    ("8% below entry"), or empty input — a protective order must never be
    submitted from a level we are not sure about.
    """
    if not guidance:
        return None
    # Only a standalone absolute price is unambiguous. Never extract the
    # "2" in "2 ATR below $100", dates, ranges or percentages.
    match = re.fullmatch(r"\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*", guidance)
    if not match:
        return None
    import math
    price = float(match.group(1).replace(",", ""))
    return price if math.isfinite(price) and price > 0 else None


class EntryPolicy(BaseModel):
    """Explicit execution contract. WAIT is the safe default, never inferred from prose."""
    status: Literal["WAIT", "READY"] = "WAIT"
    minimum_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    maximum_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    expires_at: Optional[str] = Field(default=None, description="UTC ISO timestamp: last permissible entry.")
    exit_by: Optional[str] = Field(default=None, description="UTC ISO timestamp: exit on first scheduled check at or after this deadline.")
    risk_fraction: float = Field(default=0.01, gt=0, le=0.03, allow_inf_nan=False,
                                description="Maximum planned loss / account equity; stop gaps may exceed it.")
    maximum_notional: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    confirmation: str = Field(default="", description="Evidence that all non-price conditions are already satisfied. Otherwise WAIT.")


class AdvisoryRating(str, Enum):
    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class ExecutableAction(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    LONG = "LONG"
    NEUTRAL = "NEUTRAL"
    SHORT = "SHORT"


class TargetPosition(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class PositionTransition(str, Enum):
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_LONG = "CLOSE_LONG"
    CLOSE_SHORT = "CLOSE_SHORT"
    REVERSE_TO_LONG = "REVERSE_TO_LONG"
    REVERSE_TO_SHORT = "REVERSE_TO_SHORT"
    HOLD_LONG = "HOLD_LONG"
    HOLD_SHORT = "HOLD_SHORT"
    STAY_NEUTRAL = "STAY_NEUTRAL"
    UNKNOWN = "UNKNOWN"


class PlannedBrokerAction(BaseModel):
    action: str = Field(description="Deterministic execution step, such as open_long or close_short.")
    order_type: str = Field(description="Broker order primitive: market, close_position, or none.")
    side: Optional[str] = Field(default=None, description="Broker side when applicable: buy or sell.")
    sizing_basis: str = Field(description="How execution should size this step.")


class RiskControls(BaseModel):
    mode: str = Field(
        default="advisory_only",
        description="advisory_only until broker protective-order execution is implemented.",
    )
    required_controls: Optional[str] = Field(default=None, description="Full risk-control guidance from the risk manager.")
    stop_loss: Optional[str] = Field(default=None, description="Stop-loss or invalidation guidance.")
    take_profit: Optional[str] = Field(default=None, description="Take-profit or target guidance.")
    stop_loss_price: Optional[float] = Field(
        default=None,
        description="Absolute stop-loss price level for broker protective orders, when known.",
    )
    take_profit_price: Optional[float] = Field(
        default=None,
        description="Absolute take-profit price level for broker protective orders, when known.",
    )
    invalidation: Optional[str] = Field(default=None, description="Setup invalidation guidance.")
    max_position_size: Optional[str] = Field(default=None, description="Position-size or max exposure guidance.")


class ExecutionConstraints(BaseModel):
    allow_shorts: bool = Field(description="Whether the user/session permits short exposure.")
    asset_class: str = Field(description="equity or crypto.")
    requires_open_market: bool = Field(description="Whether regular market hours are normally required.")
    broker_protective_orders_enabled: bool = Field(
        default=False,
        description="False when stops/targets are recorded but not submitted as broker child orders.",
    )
    warnings: list[str] = Field(default_factory=list, description="Execution constraints and safety notes.")


class OrderIntent(BaseModel):
    order_type: str = Field(description="Primary order type intended for execution.")
    order_class: str = Field(default="simple", description="simple, bracket, oco, or oto.")
    side: Optional[str] = Field(default=None, description="Primary broker side: buy or sell.")
    sizing_basis: str = Field(description="configured_notional, current_position, or no_order.")
    notional_usd: Optional[float] = Field(default=None, description="Configured notional, if known.")
    quantity: Optional[float] = Field(default=None, description="Explicit quantity, if known.")
    time_in_force: Optional[str] = Field(default=None, description="Broker time in force, if known.")


class TradeIntent(BaseModel):
    """Machine-readable execution contract consumed by the execution engine."""

    schema_version: str = Field(default="2.0")
    symbol: str
    trade_date: Optional[str] = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    trading_mode: str = Field(description="investment or trading.")
    action: ExecutableAction = Field(description="Final executable signal.")
    current_position: TargetPosition
    target_position: TargetPosition
    position_transition: PositionTransition
    confidence: str = Field(description="Confidence level copied from the risk decision.")
    advisory_rating: Optional[AdvisoryRating] = None
    order_intent: OrderIntent
    planned_actions: list[PlannedBrokerAction] = Field(default_factory=list)
    risk_controls: RiskControls = Field(default_factory=RiskControls)
    execution_constraints: ExecutionConstraints
    entry_policy: EntryPolicy = Field(default_factory=EntryPolicy)
    entry_guidance: Optional[str] = None
    time_horizon: Optional[str] = None
    rationale_summary: str = Field(description="Compact rationale suitable for audit logs.")


class ResearchPlan(BaseModel):
    recommendation: ExecutableAction = Field(description="Executable action for the trader.")
    confidence: str = Field(description="Confidence level: high, medium, or low.")
    advisory_rating: Optional[AdvisoryRating] = Field(default=None, description="Optional 5-tier advisory rating.")
    rationale: str = Field(description="Evidence-backed rationale from the bull/bear debate.")
    strategic_actions: str = Field(description="Concrete trading instructions and risk considerations.")


class TraderProposal(BaseModel):
    action: ExecutableAction = Field(description="Executable transaction action.")
    confidence: str = Field(description="Confidence level: high, medium, or low.")
    reasoning: str = Field(description="Concise reasoning anchored in the analysis packet.")
    entry_price: Optional[str] = Field(default=None, description="Entry guidance or price range.")
    stop_loss: Optional[str] = Field(default=None, description="Stop or invalidation guidance.")
    targets: Optional[str] = Field(default=None, description="Profit targets.")
    position_sizing: Optional[str] = Field(default=None, description="Position sizing guidance.")
    advisory_rating: Optional[AdvisoryRating] = Field(default=None, description="Optional 5-tier advisory rating.")


class RiskDecision(BaseModel):
    entry_policy: EntryPolicy = Field(default_factory=EntryPolicy)
    stop_loss_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    take_profit_price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    action: ExecutableAction = Field(description="Final executable action for Alpaca.")
    confidence: str = Field(description="Confidence level: high, medium, or low.")
    risk_rationale: str = Field(description="Risk-adjusted justification.")
    required_controls: str = Field(description="Stops, invalidation, sizing, and risk controls.")
    advisory_rating: Optional[AdvisoryRating] = Field(default=None, description="Optional 5-tier advisory rating.")
    entry_guidance: Optional[str] = Field(default=None, description="Entry price or confirmation guidance.")
    stop_loss: Optional[str] = Field(default=None, description="Structured stop-loss guidance if available.")
    take_profit: Optional[str] = Field(default=None, description="Structured take-profit or target guidance if available.")
    invalidation: Optional[str] = Field(default=None, description="Structured invalidation condition if available.")
    max_position_size: Optional[str] = Field(default=None, description="Structured max position size or risk budget.")
    time_horizon: Optional[str] = Field(default=None, description="Expected holding period or review window.")


def _rating_line(rating: Optional[AdvisoryRating]) -> list[str]:
    return ["", f"**Advisory Rating**: {rating.value}"] if rating else []


def render_research_plan(plan: ResearchPlan) -> str:
    parts = [
        f"**Recommendation**: {plan.recommendation.value}",
        f"**Confidence**: {plan.confidence}",
        *_rating_line(plan.advisory_rating),
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
        "",
        f"FINAL TRANSACTION PROPOSAL: **{plan.recommendation.value}**",
    ]
    return "\n".join(parts)


def render_trader_proposal(proposal: TraderProposal) -> str:
    parts = [
        f"**Action**: {proposal.action.value}",
        f"**Confidence**: {proposal.confidence}",
        *_rating_line(proposal.advisory_rating),
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price:
        parts.extend(["", f"**Entry**: {proposal.entry_price}"])
    if proposal.stop_loss:
        parts.extend(["", f"**Stop / Invalidation**: {proposal.stop_loss}"])
    if proposal.targets:
        parts.extend(["", f"**Targets**: {proposal.targets}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    parts.extend(["", f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value}**"])
    return "\n".join(parts)


def render_risk_decision(decision: RiskDecision) -> str:
    parts = [
        f"**Action**: {decision.action.value}",
        f"**Confidence**: {decision.confidence}",
        *_rating_line(decision.advisory_rating),
        "",
        f"**Risk Rationale**: {decision.risk_rationale}",
        "",
        f"**Required Controls**: {decision.required_controls}",
    ]
    if decision.entry_guidance:
        parts.extend(["", f"**Entry Guidance**: {decision.entry_guidance}"])
    if decision.stop_loss:
        parts.extend(["", f"**Stop Loss**: {decision.stop_loss}"])
    if decision.take_profit:
        parts.extend(["", f"**Take Profit**: {decision.take_profit}"])
    if decision.invalidation:
        parts.extend(["", f"**Invalidation**: {decision.invalidation}"])
    if decision.max_position_size:
        parts.extend(["", f"**Max Position Size**: {decision.max_position_size}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    parts.extend(["", f"FINAL TRANSACTION PROPOSAL: **{decision.action.value}**"])
    return "\n".join(parts)


def trade_intent_action(intent: Optional[dict | TradeIntent]) -> Optional[str]:
    if not intent:
        return None
    if isinstance(intent, TradeIntent):
        return intent.action.value
    action = intent.get("action") if isinstance(intent, dict) else None
    if isinstance(action, ExecutableAction):
        return action.value
    return str(action).upper() if action else None


def _normalize_position(position: Optional[str]) -> TargetPosition:
    value = (position or "NEUTRAL").upper()
    if value == "LONG":
        return TargetPosition.LONG
    if value == "SHORT":
        return TargetPosition.SHORT
    return TargetPosition.NEUTRAL


def _target_position(
    action: ExecutableAction,
    trading_mode: str,
    current_position: TargetPosition,
) -> TargetPosition:
    mode = (trading_mode or "investment").lower()
    if mode == "trading":
        if action == ExecutableAction.LONG:
            return TargetPosition.LONG
        if action == ExecutableAction.SHORT:
            return TargetPosition.SHORT
        if action == ExecutableAction.NEUTRAL:
            return TargetPosition.NEUTRAL
        return current_position

    if action == ExecutableAction.BUY:
        return TargetPosition.LONG
    if action == ExecutableAction.SELL:
        return TargetPosition.NEUTRAL
    return current_position


def _position_transition(
    current_position: TargetPosition,
    target_position: TargetPosition,
) -> PositionTransition:
    transition_map = {
        (TargetPosition.NEUTRAL, TargetPosition.LONG): PositionTransition.OPEN_LONG,
        (TargetPosition.NEUTRAL, TargetPosition.SHORT): PositionTransition.OPEN_SHORT,
        (TargetPosition.NEUTRAL, TargetPosition.NEUTRAL): PositionTransition.STAY_NEUTRAL,
        (TargetPosition.LONG, TargetPosition.LONG): PositionTransition.HOLD_LONG,
        (TargetPosition.LONG, TargetPosition.NEUTRAL): PositionTransition.CLOSE_LONG,
        (TargetPosition.LONG, TargetPosition.SHORT): PositionTransition.REVERSE_TO_SHORT,
        (TargetPosition.SHORT, TargetPosition.SHORT): PositionTransition.HOLD_SHORT,
        (TargetPosition.SHORT, TargetPosition.NEUTRAL): PositionTransition.CLOSE_SHORT,
        (TargetPosition.SHORT, TargetPosition.LONG): PositionTransition.REVERSE_TO_LONG,
    }
    return transition_map.get((current_position, target_position), PositionTransition.UNKNOWN)


def _planned_actions(transition: PositionTransition) -> list[PlannedBrokerAction]:
    action_map = {
        PositionTransition.OPEN_LONG: [
            PlannedBrokerAction(
                action="open_long",
                order_type="market",
                side="buy",
                sizing_basis="configured_notional",
            )
        ],
        PositionTransition.OPEN_SHORT: [
            PlannedBrokerAction(
                action="open_short",
                order_type="market",
                side="sell",
                sizing_basis="configured_notional",
            )
        ],
        PositionTransition.CLOSE_LONG: [
            PlannedBrokerAction(
                action="close_long",
                order_type="close_position",
                side="sell",
                sizing_basis="current_position",
            )
        ],
        PositionTransition.CLOSE_SHORT: [
            PlannedBrokerAction(
                action="close_short",
                order_type="close_position",
                side="buy",
                sizing_basis="current_position",
            )
        ],
        PositionTransition.REVERSE_TO_SHORT: [
            PlannedBrokerAction(
                action="close_long",
                order_type="close_position",
                side="sell",
                sizing_basis="current_position",
            ),
            PlannedBrokerAction(
                action="open_short",
                order_type="market",
                side="sell",
                sizing_basis="configured_notional",
            ),
        ],
        PositionTransition.REVERSE_TO_LONG: [
            PlannedBrokerAction(
                action="close_short",
                order_type="close_position",
                side="buy",
                sizing_basis="current_position",
            ),
            PlannedBrokerAction(
                action="open_long",
                order_type="market",
                side="buy",
                sizing_basis="configured_notional",
            ),
        ],
    }
    if transition in (PositionTransition.HOLD_LONG, PositionTransition.HOLD_SHORT, PositionTransition.STAY_NEUTRAL):
        return [
            PlannedBrokerAction(
                action="hold",
                order_type="none",
                side=None,
                sizing_basis="no_order",
            )
        ]
    return action_map.get(transition, [])


def _primary_order_intent(planned_actions: list[PlannedBrokerAction], asset_class: str) -> OrderIntent:
    actionable = [action for action in planned_actions if action.order_type != "none"]
    if not actionable:
        return OrderIntent(
            order_type="none",
            order_class="simple",
            side=None,
            sizing_basis="no_order",
            time_in_force=None,
        )

    primary = actionable[-1]
    time_in_force = "gtc" if asset_class == "crypto" else "day"
    return OrderIntent(
        order_type=primary.order_type,
        order_class="simple",
        side=primary.side,
        sizing_basis=primary.sizing_basis,
        time_in_force=time_in_force,
    )


def build_trade_intent_from_risk_decision(
    *,
    symbol: str,
    trading_mode: str,
    current_position: str,
    decision: RiskDecision,
    allow_shorts: bool = False,
    trade_date: Optional[str] = None,
) -> TradeIntent:
    current = _normalize_position(current_position)
    target = _target_position(decision.action, trading_mode, current)
    transition = _position_transition(current, target)
    planned = _planned_actions(transition)
    asset_class = "crypto" if "/" in (symbol or "") else "equity"

    warnings: list[str] = []
    if asset_class == "crypto" and target == TargetPosition.SHORT:
        warnings.append("Crypto short exposure is not supported by Alpaca spot trading.")
    if target == TargetPosition.SHORT and not allow_shorts:
        warnings.append("Short exposure is disabled for this session.")

    stop_loss_price = decision.stop_loss_price or extract_protective_price(decision.stop_loss)
    take_profit_price = decision.take_profit_price or extract_protective_price(decision.take_profit)
    has_numeric_controls = bool(stop_loss_price or take_profit_price)
    if (decision.required_controls or decision.stop_loss or decision.take_profit) and not has_numeric_controls:
        warnings.append(
            "Risk controls carry no absolute protective-order price levels; they remain advisory metadata."
        )

    risk_controls = RiskControls(
        mode="advisory_only",
        required_controls=decision.required_controls,
        stop_loss=decision.stop_loss,
        take_profit=decision.take_profit,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        invalidation=decision.invalidation,
        max_position_size=decision.max_position_size,
    )
    constraints = ExecutionConstraints(
        allow_shorts=allow_shorts,
        asset_class=asset_class,
        requires_open_market=asset_class != "crypto",
        broker_protective_orders_enabled=has_numeric_controls and asset_class != "crypto",
        warnings=warnings,
    )

    return TradeIntent(
        symbol=symbol,
        trade_date=trade_date,
        trading_mode=(trading_mode or "investment").lower(),
        action=decision.action,
        current_position=current,
        target_position=target,
        position_transition=transition,
        confidence=decision.confidence,
        advisory_rating=decision.advisory_rating,
        order_intent=_primary_order_intent(planned, asset_class),
        planned_actions=planned,
        risk_controls=risk_controls,
        execution_constraints=constraints,
        entry_policy=decision.entry_policy,
        entry_guidance=decision.entry_guidance,
        time_horizon=decision.time_horizon,
        rationale_summary=decision.risk_rationale,
    )
