from datetime import datetime, timezone
from ..schemas import (
    ExecutableAction,
    RiskDecision,
    build_trade_intent_from_risk_decision,
    render_risk_decision,
)
from ..utils.agent_trading_modes import (
    ensure_final_transaction_proposal,
    get_trading_mode_context,
    get_agent_specific_context,
    extract_recommendation,
)
from ..utils.memory import TradingMemoryLog
from ..utils.report_context import (
    get_agent_context_bundle,
    build_debate_digest,
)
from ..utils.structured import bind_structured, invoke_risk_structured_strict
from tradingagents.dataflows.interface_utils import (
    HISTORICAL_SOURCE_UNAVAILABLE,
    analysis_date_mode,
)
from tradingagents.execution.context import (
    capture_position_context,
    render_account_context,
    render_position_context,
)
from tradingagents.prompts import render_prompt

# Import prompt capture utility
try:
    from webui.utils.prompt_capture import capture_agent_prompt
except ImportError:
    # Fallback for when webui is not available
    def capture_agent_prompt(report_type, prompt_content, symbol=None):
        pass


def create_risk_manager(llm, memory, config=None):
    structured_llm = bind_structured(llm, RiskDecision, "Risk Manager")
    decision_log = TradingMemoryLog(config)

    def risk_manager_node(state) -> dict:

        company_name = state["company_of_interest"]

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        trader_plan = state["trader_investment_plan"]

        # Get trading mode from config
        allow_shorts = config.get("allow_shorts", False) if config else False

        # Phase B: the Decision node re-captures a fresh authoritative
        # snapshot at its own start — holdings may have changed during the
        # debate. The capture is bound to the account the Trader observed:
        # an account switch between the two prompts stops the run. Capture
        # failures raise BrokerAuthorityError; unknown holdings are never
        # presented as flat. This prompt snapshot never bypasses the
        # executor's pre-submit revalidation, which takes its own
        # authoritative snapshot inside the lock.
        #
        # Historical as-of runs never call the live broker. Without a
        # verified point-in-time portfolio context the Decision role fails
        # closed: no opening READY intent, output is NO_TRADE / HOLD with an
        # explicit unavailability explanation.
        if analysis_date_mode(state.get("trade_date")) == "historical":
            historical_position = state.get("current_position")
            historical_position_stats = state.get("position_stats")
            historical_account_status = state.get("account_status")
            historical_context_trusted = (
                bool(historical_position)
                and historical_position != HISTORICAL_SOURCE_UNAVAILABLE
                and bool(historical_position_stats)
                and bool(historical_account_status)
            )
            if not historical_context_trusted:
                trading_mode = "trading" if allow_shorts else "investment"
                neutral_action = "NO_TRADE" if allow_shorts else "HOLD"
                final_decision_content = (
                    f"NO_TRADE — historical portfolio context unavailable "
                    f"({HISTORICAL_SOURCE_UNAVAILABLE}). Without verified as-of "
                    "position/account data no opening intent can be authorized; "
                    "the live broker was not consulted for this historical date."
                    f"\n\nFINAL TRANSACTION PROPOSAL: **{neutral_action}**"
                )
                new_risk_debate_state = {
                    "judge_decision": final_decision_content,
                    "history": risk_debate_state["history"],
                    "risky_history": risk_debate_state["risky_history"],
                    "safe_history": risk_debate_state["safe_history"],
                    "neutral_history": risk_debate_state["neutral_history"],
                    "risky_messages": risk_debate_state.get("risky_messages", []),
                    "safe_messages": risk_debate_state.get("safe_messages", []),
                    "neutral_messages": risk_debate_state.get("neutral_messages", []),
                    "latest_speaker": "Judge",
                    "current_risky_response": risk_debate_state["current_risky_response"],
                    "current_safe_response": risk_debate_state["current_safe_response"],
                    "current_neutral_response": risk_debate_state["current_neutral_response"],
                    "count": risk_debate_state["count"],
                }
                return {
                    "risk_debate_state": new_risk_debate_state,
                    "final_trade_decision": final_decision_content,
                    "final_trade_intent": None,
                    "trading_mode": trading_mode,
                    "current_position": historical_position
                    or HISTORICAL_SOURCE_UNAVAILABLE,
                    "recommended_action": neutral_action,
                    "risk_invalid_reason": "historical_portfolio_context_unavailable",
                }
            current_position = historical_position
            position_stats_desc = historical_position_stats
            account_status_desc = historical_account_status
            state["current_position"] = current_position
        else:
            expected_account = state.get("broker_account_id")
            context = capture_position_context(
                company_name, expected_account_id=expected_account
            )
            position_stats_desc = render_position_context(context)
            account_status_desc = render_account_context(context)

            current_position = context.side if context.side != "FLAT" else "NEUTRAL"
            state["current_position"] = current_position
            state["broker_account_id"] = context.account_id

        open_pos_desc = (
            f"We currently have an open {current_position} position in {company_name}."
            if current_position != "NEUTRAL"
            else f"We do not have any open position in {company_name}."
        )
        
        # Get centralized trading mode context
        trading_context = get_trading_mode_context(config, current_position)
        agent_context = get_agent_specific_context("manager", trading_context)
        
        # Get mode-specific terms for the prompt
        actions = trading_context["actions"]
        mode_name = trading_context["mode_name"]
        decision_format = trading_context["decision_format"]
        final_format = trading_context["final_format"]
        output_language = (config or {}).get("output_language", "English")
        context_bundle = get_agent_context_bundle(
            state,
            agent_role="managers/risk_manager",
            objective=(
                f"Judge risk debate and finalize risk-adjusted trade decision for {company_name}. "
                f"Trader plan: {trader_plan}"
            ),
            config=config,
        )
        claim_matrix = context_bundle.get("decision_claim_matrix", "")
        risk_debate_digest = build_debate_digest(risk_debate_state, "risk", config=config)
        all_reports_text = context_bundle.get("all_reports_text", "")

        curr_situation = context_bundle["memory_context"]
        past_memories = memory.get_memories(curr_situation, n_matches=2, as_of=state.get("trade_date"))

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"
        decision_memory_str = decision_log.get_past_context(company_name, as_of=state.get("trade_date"))

        prompt = render_prompt(
            "managers/risk_manager",
            agent_context=agent_context,
            decision_time_utc=datetime.now(timezone.utc).isoformat(),
            analysis_date=state.get("trade_date", "unknown"),
            decision_format=decision_format,
            open_pos_desc=open_pos_desc,
            position_stats_desc=position_stats_desc,
            account_status_desc=account_status_desc,
            trader_plan=trader_plan,
            claim_matrix=claim_matrix,
            all_reports_text=all_reports_text,
            risk_debate_digest=risk_debate_digest,
            history=history,
            past_memory_str=past_memory_str,
            decision_memory_str=decision_memory_str,
            actions=actions,
            final_format=final_format,
            output_language=output_language,
        )

        # Capture the COMPLETE prompt that gets sent to the LLM
        capture_agent_prompt("final_trade_decision", prompt, company_name)

        # Strict Risk boundary (Phase A.1): structured bind/invoke/
        # validation/timeout/provider/empty/illegal failures all emit
        # INVALID/NO_TRADE. Free-text/Markdown/regex must never become a
        # tradable action. Analyst/Research/Trader keep their fallbacks;
        # only risk is strict because only risk produces TradeIntent.
        trading_mode = trading_context["mode"]
        neutral_action = "NEUTRAL" if trading_mode == "trading" else "HOLD"
        response_content, structured_decision, strict_error = invoke_risk_structured_strict(
            structured_llm,
            prompt,
            render_risk_decision,
            "Risk Manager",
            schema=RiskDecision,
        )

        if strict_error is not None or structured_decision is None:
            reason = strict_error or "structured_unavailable"
            final_decision_content = (
                f"INVALID risk decision ({reason}). NO_TRADE.\n\n"
                f"Structured RiskDecision was unavailable; no action is inferred "
                f"from free text. Review manually.\n\n"
                f"FINAL TRANSACTION PROPOSAL: **{neutral_action}**"
            )
            trade_intent = None
            extracted_recommendation = neutral_action
            new_risk_debate_state = {
                "judge_decision": final_decision_content,
                "history": risk_debate_state["history"],
                "risky_history": risk_debate_state["risky_history"],
                "safe_history": risk_debate_state["safe_history"],
                "neutral_history": risk_debate_state["neutral_history"],
                "risky_messages": risk_debate_state.get("risky_messages", []),
                "safe_messages": risk_debate_state.get("safe_messages", []),
                "neutral_messages": risk_debate_state.get("neutral_messages", []),
                "latest_speaker": "Judge",
                "current_risky_response": risk_debate_state["current_risky_response"],
                "current_safe_response": risk_debate_state["current_safe_response"],
                "current_neutral_response": risk_debate_state["current_neutral_response"],
                "count": risk_debate_state["count"],
            }
            return {
                "risk_debate_state": new_risk_debate_state,
                "final_trade_decision": final_decision_content,
                "final_trade_intent": trade_intent,
                "trading_mode": trading_mode,
                "current_position": current_position,
                "recommended_action": extracted_recommendation,
                "risk_invalid_reason": reason,
            }

        # Structured success: still fail closed on illegal cross-mode actions.
        allowed_actions = (
            {"LONG", "NEUTRAL", "SHORT"}
            if trading_mode == "trading"
            else {"BUY", "HOLD", "SELL"}
        )
        action_value = structured_decision.action.value
        if action_value not in allowed_actions:
            reason = f"illegal_action_for_mode:{action_value}:{trading_mode}"
            final_decision_content = (
                f"INVALID risk decision ({reason}). NO_TRADE.\n\n"
                f"FINAL TRANSACTION PROPOSAL: **{neutral_action}**"
            )
            new_risk_debate_state = {
                "judge_decision": final_decision_content,
                "history": risk_debate_state["history"],
                "risky_history": risk_debate_state["risky_history"],
                "safe_history": risk_debate_state["safe_history"],
                "neutral_history": risk_debate_state["neutral_history"],
                "risky_messages": risk_debate_state.get("risky_messages", []),
                "safe_messages": risk_debate_state.get("safe_messages", []),
                "neutral_messages": risk_debate_state.get("neutral_messages", []),
                "latest_speaker": "Judge",
                "current_risky_response": risk_debate_state["current_risky_response"],
                "current_safe_response": risk_debate_state["current_safe_response"],
                "current_neutral_response": risk_debate_state["current_neutral_response"],
                "count": risk_debate_state["count"],
            }
            return {
                "risk_debate_state": new_risk_debate_state,
                "final_trade_decision": final_decision_content,
                "final_trade_intent": None,
                "trading_mode": trading_mode,
                "current_position": current_position,
                "recommended_action": neutral_action,
                "risk_invalid_reason": reason,
            }

        extracted_recommendation = action_value
        final_decision_content = ensure_final_transaction_proposal(
            response_content, extracted_recommendation, trading_mode
        )

        trade_intent = build_trade_intent_from_risk_decision(
            symbol=company_name,
            trading_mode=trading_mode,
            current_position=current_position,
            decision=structured_decision,
            allow_shorts=allow_shorts,
            trade_date=state.get("trade_date"),
        ).model_dump(mode="json")

        new_risk_debate_state = {
            "judge_decision": final_decision_content,
            "history": risk_debate_state["history"],
            "risky_history": risk_debate_state["risky_history"],
            "safe_history": risk_debate_state["safe_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "risky_messages": risk_debate_state.get("risky_messages", []),
            "safe_messages": risk_debate_state.get("safe_messages", []),
            "neutral_messages": risk_debate_state.get("neutral_messages", []),
            "latest_speaker": "Judge",
            "current_risky_response": risk_debate_state["current_risky_response"],
            "current_safe_response": risk_debate_state["current_safe_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_decision_content,
            "final_trade_intent": trade_intent,
            "trading_mode": trading_mode,
            "current_position": current_position,
            "recommended_action": extracted_recommendation,
        }

    return risk_manager_node
