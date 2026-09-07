"""Shared auto-trade preparation (WebUI + Phase D long-run).

Single home for the non-UI sizing/execution preparation previously embedded
in the WebUI: typed ``TradeIntent`` required (fail-closed otherwise),
regime-aware sizing then portfolio-intelligence sizing for opening long
exposure, then one ``ExecutionService.execute`` call. Sizing only shrinks;
failures keep the requested amount untouched.
"""

from __future__ import annotations

from typing import Any, Optional


def execute_auto_trade(
    *,
    ticker: str,
    trade_intent: Any,
    base_trade_notional_usd: float,
    allow_shorts: bool,
    config: Optional[dict] = None,
    execution_service: Any = None,
    decision_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Prepare and execute one auto trade through the single execution entry."""
    from tradingagents.agents.schemas import trade_intent_action

    if trade_intent is None:
        return {
            "success": False,
            "fail_closed": True,
            "broker_attempted": False,
            "broker_calls": 0,
            "error": (
                "Missing schema-valid TradeIntent; trade skipped fail-closed. "
                "Legacy signal execution is disabled."
            ),
        }

    if config is None:
        from tradingagents.dataflows.config import get_config
        config = get_config() or {}
    action = trade_intent_action(trade_intent)
    amount = float(base_trade_notional_usd or 0.0)

    if action and str(action).upper() in ("BUY", "LONG"):
        if config is None:
            try:
                from tradingagents.dataflows.config import get_config

                config = get_config() or {}
            except Exception:
                config = {}
        try:
            from tradingagents.regime import RegimeConfig, regime_risk_multiplier

            multiplier = regime_risk_multiplier(
                ticker, config=RegimeConfig.from_config(config)
            )
            if multiplier < 1.0:
                amount = amount * multiplier
        except Exception:
            pass
        try:
            from tradingagents.portfolio import (
                PortfolioLimitsConfig,
                adjust_new_position_notional,
                gather_portfolio_state_via_alpaca,
            )

            amount = adjust_new_position_notional(
                ticker,
                action,
                amount,
                gather_state=gather_portfolio_state_via_alpaca,
                config=PortfolioLimitsConfig.from_config(config or {}),
            )
        except Exception:
            pass

    service = execution_service
    if service is None:
        from tradingagents.execution.service import ExecutionService

        service = ExecutionService()
    return service.execute(
        trade_intent=trade_intent,
        decision_id=decision_id,
        run_id=run_id,
        dollar_amount=amount,
        allow_shorts=allow_shorts,
        risk_params=(config.get("risk_sizing_params") or {}) if config.get("risk_sizing_enabled", False) else None,
    )
