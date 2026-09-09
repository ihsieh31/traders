# TradingAgents/graph/trading_graph.py

import os
from pathlib import Path
import json
from datetime import date, datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional

import yfinance as yf
from langgraph.prebuilt import ToolNode

from tradingagents.llm_clients import create_llm_client
from tradingagents.llm_clients.retry import (
    FailoverRetryingLLM,
    ProviderFailure,
    RetryingLLM,
    validate_llm_max_retries,
)
from tradingagents.llm_clients.roles import describe_roles, resolve_role_config
from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import FinancialSituationMemory, TradingMemoryLog
from tradingagents.agents.schemas import trade_intent_action
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.openai_model_registry import normalize_model_params, describe_model_params
from tradingagents.run_logger import get_run_audit_logger
from tradingagents.dataflows.config import (
    get_llm_api_key,
    get_openai_base_url,
    is_local_openai_enabled,
    set_config,
)
from tradingagents.dataflows.ticker_utils import TickerUtils, is_crypto_ticker
from tradingagents.dataflows.utils import safe_ticker_component

from .checkpointer import clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "macro"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config.get("results_dir", "eval_results"), exist_ok=True)

        # Initialize LLMs with appropriate parameters based on model type and research depth
        deep_think_model = self.config["deep_think_llm"]
        quick_think_model = self.config["quick_think_llm"]
        
        # Research depth now controls debate rounds. Model parameters are explicit
        # per selected model and can be adjusted in the UI.
        research_depth = self.config.get("research_depth", "Medium")
        
        # Convert integer research_depth (from debate rounds) back to string if needed
        if isinstance(research_depth, int):
            depth_map = {1: "Shallow", 2: "Medium", 3: "Deep"}
            research_depth = depth_map.get(research_depth, "Medium")
        
        quick_think_kwargs = normalize_model_params(
            quick_think_model,
            self.config.get("quick_llm_params"),
            role="quick",
        )
        deep_think_kwargs = normalize_model_params(
            deep_think_model,
            self.config.get("deep_llm_params"),
            role="deep",
        )
        
        # Log the configuration being used
        quick_params_desc = describe_model_params(quick_think_model, quick_think_kwargs, "quick")
        deep_params_desc = describe_model_params(deep_think_model, deep_think_kwargs, "deep")
        print(f"[LLM CONFIG] Research Depth: {research_depth} (debate rounds only)")
        print(f"[LLM CONFIG] Quick Thinker ({quick_think_model}): {quick_params_desc}")
        print(f"[LLM CONFIG] Deep Thinker ({deep_think_model}): {deep_params_desc}")

        provider = self.config.get("llm_provider", "openai").lower()
        backend_url = self.config.get("backend_url")
        if provider == "openai" and is_local_openai_enabled():
            provider = "local_openai"
            backend_url = backend_url or get_openai_base_url()
        elif provider == "local_openai":
            backend_url = backend_url or get_openai_base_url()
        if backend_url:
            print(f"[LLM CONFIG] Using OpenAI-compatible endpoint: {backend_url}")

        base_llm_kwargs = self._get_provider_kwargs(provider)
        if self.callbacks:
            base_llm_kwargs["callbacks"] = self.callbacks

        # Phase B: bounded LLM retry policy (integer 0-3, validated at
        # startup) and the Analysis/Decision role split. With no role keys
        # set the legacy quick/deep behavior is preserved exactly.
        try:
            llm_max_retries = validate_llm_max_retries(
                self.config.get("llm_max_retries", 3)
            )
        except ValueError as exc:
            raise ValueError(f"Invalid LLM retry configuration: {exc}") from exc
        self.llm_request_timeout_seconds = float(
            self.config.get("llm_request_timeout_seconds", 120.0)
        )
        # F08: one configuration key, same finite timeout, every production
        # LLM client path (legacy quick/deep, Analysis/Decision roles,
        # fallback, Screening, GPT-5 Responses adapter).
        base_llm_kwargs.setdefault("timeout", self.llm_request_timeout_seconds)

        self.role_resolution = resolve_role_config(self.config)
        self.llm_max_retries = llm_max_retries

        if self.role_resolution["mode"] == "roles":
            analysis_spec = self.role_resolution["analysis"]
            decision_spec = self.role_resolution["decision"]
            print(f"[LLM CONFIG] Roles: {describe_roles(self.role_resolution)}")
            print(
                f"[LLM CONFIG] Retry: max {llm_max_retries} retries "
                f"(<= {1 + llm_max_retries} requests per invocation)"
            )
            fallback_spec = self.role_resolution.get("analysis_fallback")
            fallback_key = self.role_resolution.get("analysis_fallback_api_key") or ""
            if fallback_spec is None:
                analysis_client = self._build_role_client(
                    analysis_spec, self.role_resolution["analysis_api_key"]
                )
            else:
                # Primary→Fallback failover with one shared request budget.
                # The Analysis and Decision roles both consume the same
                # fallback route (quota exhaustion or transient provider
                # failure must not kill a Phase-D round at the Risk Manager,
                # the last call of every ticker). Screening builds its own
                # failover wrapper in screening.llm.
                analysis_client = self._build_role_client(
                    analysis_spec,
                    self.role_resolution["analysis_api_key"],
                    fallback_spec=fallback_spec,
                    fallback_api_key=fallback_key,
                )
            decision_client = self._build_role_client(
                decision_spec,
                self.role_resolution["decision_api_key"],
                fallback_spec=fallback_spec,
                fallback_api_key=fallback_key,
            )
            self.deep_thinking_llm = analysis_client
            self.quick_thinking_llm = analysis_client
            self.decision_llm = decision_client
        else:
            deep_client = create_llm_client(
                provider=provider,
                model=deep_think_model,
                base_url=backend_url,
                api_key=get_llm_api_key(provider),
                model_role="deep",
                **base_llm_kwargs,
                **deep_think_kwargs,
            )
            quick_client = create_llm_client(
                provider=provider,
                model=quick_think_model,
                base_url=backend_url,
                api_key=get_llm_api_key(provider),
                model_role="quick",
                **base_llm_kwargs,
                **quick_think_kwargs,
            )

            self.deep_thinking_llm = deep_client.get_llm()
            self.quick_thinking_llm = quick_client.get_llm()
            self.decision_llm = None
            # Phase B: wrap both legacy clients with the single bounded
            # retry owner (role label is legacy because the deep client is
            # shared between research nodes and the Risk Manager).
            self.deep_thinking_llm = RetryingLLM(
                self.deep_thinking_llm,
                role="legacy-deep",
                provider=provider,
                model=deep_think_model,
                max_retries=llm_max_retries,
            )
            self.quick_thinking_llm = RetryingLLM(
                self.quick_thinking_llm,
                role="legacy-quick",
                provider=provider,
                model=quick_think_model,
                max_retries=llm_max_retries,
            )
        
        self.toolkit = Toolkit(config=self.config)

        # Initialize memories (persistent when agent_memory_dir is configured)
        self.bull_memory = FinancialSituationMemory("bull_memory", self.config)
        self.bear_memory = FinancialSituationMemory("bear_memory", self.config)
        self.trader_memory = FinancialSituationMemory("trader_memory", self.config)
        self.invest_judge_memory = FinancialSituationMemory("invest_judge_memory", self.config)
        self.risk_manager_memory = FinancialSituationMemory("risk_manager_memory", self.config)
        self.memory_log = TradingMemoryLog(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config.get("max_debate_rounds", 2), 
            max_risk_discuss_rounds=self.config.get("max_risk_discuss_rounds", 2)
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.toolkit,
            self.tool_nodes,
            self.bull_memory,
            self.bear_memory,
            self.trader_memory,
            self.invest_judge_memory,
            self.risk_manager_memory,
            self.conditional_logic,
            self.config,
            decision_llm=self.decision_llm,
        )

        self.propagator = Propagator(max_recur_limit=self.config.get("max_recur_limit", 200))
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _build_role_inner(self, spec, api_key: str):
        """Build the unwrapped LLM for one role spec: provider kwargs, model
        params, client construction. Retry wrapping stays with the caller so
        the failover path can wrap a Primary/Fallback pair with one shared
        owner instead of two stacked retry layers."""
        provider = spec.provider
        provider_kwargs = self._get_provider_kwargs(provider)
        if self.callbacks:
            provider_kwargs["callbacks"] = self.callbacks
        params = normalize_model_params(
            spec.model,
            self.config.get("deep_llm_params"),
            role="deep",
        )
        # Per-role kwargs: explicit model params win over the provider-level
        # switches so OpenAI reasoning never leaks into Google/Anthropic.
        merged_kwargs = {**provider_kwargs, **params}
        # F08: role clients honor the same configured request timeout as the
        # legacy quick/deep path (overridable by explicit model params).
        merged_kwargs.setdefault("timeout", self.llm_request_timeout_seconds)
        client = create_llm_client(
            provider=provider,
            model=spec.model,
            base_url=spec.backend_url,
            api_key=api_key,
            model_role="deep",
            **merged_kwargs,
        )
        return client.get_llm()

    def _build_role_client(
        self,
        spec,
        api_key: str,
        fallback_spec=None,
        fallback_api_key: str = "",
    ):
        """Build one role client (Analysis or Decision) with isolated
        provider/model/endpoint/credential and the bounded retry owner.
        When a fallback route is supplied the client becomes a
        Primary→FailoverRetryingLLM wrapper sharing the one request budget
        across both routes (attempt 1 is always Primary)."""
        inner = self._build_role_inner(spec, api_key)
        if fallback_spec is None:
            return RetryingLLM(
                inner,
                role=spec.role,
                provider=spec.provider,
                model=spec.model,
                max_retries=self.llm_max_retries,
            )
        return FailoverRetryingLLM(
            inner,
            self._build_role_inner(fallback_spec, fallback_api_key),
            role=spec.role,
            primary_provider=spec.provider,
            primary_model=spec.model,
            fallback_provider=fallback_spec.provider,
            fallback_model=fallback_spec.model,
            max_retries=self.llm_max_retries,
        )

    def _get_provider_kwargs(self, provider: str) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        provider = (provider or "openai").lower()
        if provider == "google" and self.config.get("google_thinking_level"):
            kwargs["thinking_level"] = self.config["google_thinking_level"]
        elif provider in ("openai", "local_openai") and self.config.get("openai_reasoning_effort"):
            kwargs["reasoning_effort"] = self.config["openai_reasoning_effort"]
        elif provider == "anthropic" and self.config.get("anthropic_effort"):
            kwargs["effort"] = self.config["anthropic_effort"]
        return kwargs

    def _graph_for_run(self, ticker: str, trade_date: str):
        """Return a compiled graph and optional checkpointer context for one run."""
        if not self.config.get("checkpoint_enabled", False):
            return self.graph, None

        checkpointer_ctx = get_checkpointer(self.config["data_cache_dir"], ticker)
        checkpointer = checkpointer_ctx.__enter__()
        return self.workflow.compile(checkpointer=checkpointer), checkpointer_ctx

    def _graph_args_for_run(self, ticker: str, trade_date: str) -> Dict[str, Any]:
        args = self.propagator.get_graph_args()
        if self.config.get("checkpoint_enabled", False):
            args["config"].setdefault("configurable", {})["thread_id"] = thread_id(
                ticker, str(trade_date)
            )
        return args

    def _ticker_for_yfinance(self, ticker: str) -> str:
        try:
            return TickerUtils.convert_for_api(ticker, "yahoo")
        except Exception:
            return ticker.replace("/", "-")

    def _benchmark_for(self, ticker: str) -> Optional[str]:
        if is_crypto_ticker(ticker):
            base = self._ticker_for_yfinance(ticker).split("-")[0].upper()
            return None if base == "BTC" else "BTC-USD"
        return None if ticker.upper() == "SPY" else "SPY"

    def _fetch_return(self, ticker: str, start_date: date, holding_days: int,
                      as_of: Optional[date] = None) -> Optional[float]:
        """Fixed next-open forward asset move; never broker realized P&L."""
        cutoff = min(as_of or date.today(), date.today())
        if holding_days < 1 or cutoff <= start_date:
            return None
        try:
            data = yf.download(self._ticker_for_yfinance(ticker), start=start_date.isoformat(),
                               end=cutoff.isoformat(), progress=False, auto_adjust=True,
                               actions=False, threads=False)
            if data is None or data.empty or "Open" not in data:
                return None
            opens = data["Open"]
            if hasattr(opens, "columns"):
                opens = opens.iloc[:, 0]
            opens = opens[(opens.index.date > start_date) & (opens.index.date < cutoff)].dropna()
            if len(opens) <= holding_days:
                return None
            first, last = float(opens.iloc[0]), float(opens.iloc[holding_days])
            import math
            if not all(math.isfinite(v) and v > 0 for v in (first, last)):
                return None
            return last / first - 1.0
        except Exception:
            return None

    def _resolve_memory_log_outcomes(self, ticker: str, trade_date: str) -> None:
        holding_days = int(self.config.get("memory_outcome_holding_days", 5))
        try:
            current_date = datetime.strptime(str(trade_date), "%Y-%m-%d").date()
        except ValueError:
            current_date = date.today()

        from tradingagents.backtest.signals import load_recorded_runs
        forward_runs = load_recorded_runs(ticker, self.config.get("results_dir", "eval_results"))
        for entry in self.memory_log.get_pending_entries(ticker):
            entry_date_text = entry.get("date")
            if not entry_date_text or entry_date_text not in forward_runs:
                continue
            try:
                entry_date = datetime.strptime(entry_date_text, "%Y-%m-%d").date()
            except ValueError:
                continue
            if entry_date >= current_date:
                continue

            raw_return = self._fetch_return(ticker, entry_date, holding_days, as_of=current_date)
            if raw_return is None:
                continue

            benchmark = self._benchmark_for(ticker)
            benchmark_return = (
                self._fetch_return(benchmark, entry_date, holding_days, as_of=current_date)
                if benchmark
                else None
            )
            alpha_return = (
                raw_return - benchmark_return
                if benchmark_return is not None
                else None
            )
            reflection = (
                f"Forward asset move over {holding_days} complete bars: {raw_return:+.1%}. "
                "This is not strategy realized P&L. No inference of decision quality, "
                "trade profitability or a missed opportunity is justified without positions, fills and costs."
            )
            self.memory_log.update_with_outcome(
                ticker=ticker,
                trade_date=entry_date_text,
                raw_return=raw_return,
                alpha_return=alpha_return,
                holding_days=holding_days,
                reflection=reflection,
            )
            self._reflect_agents_on_outcome(
                ticker, entry_date_text, raw_return, alpha_return, holding_days
            )

    def _reflect_agents_on_outcome(
        self,
        ticker: str,
        trade_date: str,
        raw_return: float,
        alpha_return: Optional[float],
        holding_days: int,
    ) -> None:
        """Feed a realized outcome back into the per-agent ChromaDB memories.

        Recovers the original run's final_state from the persisted run log,
        so each agent reflects on the exact situation it saw when the
        decision was made — not on today's state. Best-effort by design:
        reflection failures must never affect the current analysis run.
        """
        if not self.config.get("reflection_on_outcome_enabled", False):
            return
        try:
            from tradingagents.run_logger import load_final_state_snapshot

            state = load_final_state_snapshot(ticker, trade_date, eval_results_dir=self.config.get("results_dir", "eval_results"))
            if not state:
                return
            alpha_text = (
                f"{alpha_return:+.1%} vs benchmark"
                if alpha_return is not None
                else "n/a"
            )
            returns_losses = (
                f"Forward asset move (NOT broker P&L) over {holding_days} completed bars: "
                f"{raw_return:+.1%} (alpha: {alpha_text})."
            )
            memories = {
                "bull": self.bull_memory,
                "bear": self.bear_memory,
                "trader": self.trader_memory,
                "invest_judge": self.invest_judge_memory,
                "risk_manager": self.risk_manager_memory,
            }
            self.reflector.reflect_on_outcome(state, returns_losses, memories)
            if self.config.get("memory_maintenance_enabled", True):
                from tradingagents.agents.utils.memory_maintenance import (
                    maintain_all_memories,
                )

                maintain_all_memories(memories, self.config)
        except Exception as exc:
            print(f"[REFLECTION] Outcome reflection skipped for {ticker} {trade_date}: {exc}")

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources."""
        news_tools = [
            self.toolkit.get_google_news,
            self.toolkit.get_finnhub_news_recent,
            self.toolkit.get_coindesk_news,
        ]
        if self.config.get("news_global_openai_enabled", False):
            news_tools.insert(0, self.toolkit.get_global_news_openai)

        return {
            "market": ToolNode(
                [
                    # Only PIT-safe Alpaca price tools may be registered here:
                    # get_alpaca_data_report bounds rows at curr_date and gates
                    # the latest quote to live mode.
                    self.toolkit.get_stockstats_indicators_report_online,
                    # offline tools
                    self.toolkit.get_stockstats_indicators_report,
                    self.toolkit.get_alpaca_data_report,
                ]
            ),
            "social": ToolNode(
                [
                    # online tools
                    self.toolkit.get_stock_news_openai,
                    # direct social tools
                    self.toolkit.get_reddit_stock_info,
                    self.toolkit.get_reddit_news,
                ]
            ),
            "news": ToolNode(news_tools),
            "fundamentals": ToolNode(
                [
                    # online tools
                    self.toolkit.get_fundamentals_openai,
                    self.toolkit.get_defillama_fundamentals,
                    # primary sources (Phase B): official SEC filings and
                    # configured company IR pages with honest date metadata
                    self.toolkit.get_sec_ir_source,
                    # direct data tools
                    self.toolkit.get_finnhub_company_insider_sentiment,
                    self.toolkit.get_finnhub_company_insider_transactions,
                    self.toolkit.get_simfin_balance_sheet,
                    self.toolkit.get_simfin_cashflow,
                    self.toolkit.get_simfin_income_stmt,
                ]
            ),
            "macro": ToolNode(
                [
                    # macro economic tools
                    self.toolkit.get_macro_analysis,
                    self.toolkit.get_economic_indicators,
                    self.toolkit.get_yield_curve_analysis,
                    self.toolkit.get_macro_news_openai,
                ]
            ),
        }

    def propagate(self, company_name, trade_date):
        """Run the trading agents graph for a company on a specific date."""

        self.ticker = company_name
        run_logger = get_run_audit_logger()
        self._resolve_memory_log_outcomes(company_name, str(trade_date))

        # Initialize state
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date
        )
        args = self._graph_args_for_run(company_name, str(trade_date))
        graph, checkpointer_ctx = self._graph_for_run(company_name, str(trade_date))
        # F12: long-run analyses carry their observation identity in the run
        # log's metadata. Crash recovery requires an exact metadata match, so
        # an unattended observation can never borrow a decision written by a
        # manual/WebUI run or a different observation. Manual CLI/WebUI runs
        # keep their own source metadata.
        metadata = {"debug": self.debug}
        for marker in ("_long_run_observation_id", "_analysis_source"):
            value = self.config.get(marker)
            if value:
                metadata[marker.lstrip("_")] = value
        run_logger.start_run(
            symbol=company_name,
            trade_date=str(trade_date),
            config=self.config,
            metadata=metadata,
        )
        run_logger.log_state_snapshot(
            stage="initial_state",
            snapshot=init_agent_state,
            symbol=company_name,
        )

        try:
            if self.debug:
                # Debug mode with tracing
                trace = []
                for chunk in graph.stream(init_agent_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)

                final_state = trace[-1]
            else:
                # Standard mode without tracing
                final_state = graph.invoke(init_agent_state, **args)
        except ProviderFailure as exc:
            # Phase B: a provider access failure stops the whole run. Record
            # the identifiable failure (role/provider/model/attempts/category,
            # sanitized) and mark the run stopped so operators see why.
            run_logger.log_event(
                event_type="provider_failure",
                symbol=company_name,
                payload={"run_stopped": True, **exc.to_dict()},
            )
            run_logger.finish_run(
                symbol=company_name,
                status="stopped",
                error_message=str(exc),
            )
            print(f"[RUN STOPPED] {company_name}: {exc}")
            raise
        except Exception as e:
            run_logger.finish_run(
                symbol=company_name,
                status="failed",
                error_message=str(e),
            )
            raise
        finally:
            if checkpointer_ctx is not None:
                checkpointer_ctx.__exit__(None, None, None)

        # Store current state for reflection
        self.curr_state = final_state

        # Log state
        self._log_state(trade_date, final_state)

        # Return decision and processed signal
        try:
            final_signal = trade_intent_action(final_state.get("final_trade_intent")) or self.process_signal(
                final_state["final_trade_decision"]
            )
            try:
                from webui.utils.state import app_state

                symbol_state = app_state.get_state(company_name) or {}
                filtered_tool_calls = [
                    call for call in app_state.tool_calls_log
                    if call.get("symbol") == company_name
                ]
                run_logger.log_state_snapshot(
                    stage="webui_runtime_context",
                    snapshot={
                        "session_id": symbol_state.get("session_id"),
                        "session_start_time": symbol_state.get("session_start_time"),
                        "agent_prompts": symbol_state.get("agent_prompts", {}),
                        "tool_calls": filtered_tool_calls,
                        "llm_calls_count": app_state.llm_calls_count,
                        "tool_calls_count": app_state.tool_calls_count,
                    },
                    symbol=company_name,
                )
            except Exception:
                pass

            run_logger.finish_run(
                symbol=company_name,
                status="completed",
                final_state=final_state,
                final_signal=final_signal,
            )
            self.memory_log.store_decision(
                ticker=company_name,
                trade_date=str(trade_date),
                final_trade_decision=final_state["final_trade_decision"],
                trading_mode=final_state.get(
                    "trading_mode",
                    self.config.get("trading_mode", "investment"),
                ),
            )
            if self.config.get("checkpoint_enabled", False):
                clear_checkpoint(self.config["data_cache_dir"], company_name, str(trade_date))
            return final_state, final_signal
        except Exception as e:
            run_logger.finish_run(
                symbol=company_name,
                status="failed",
                final_state=final_state,
                error_message=str(e),
            )
            raise

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "report_context_stats": final_state.get("report_context", {}).get("stats", {}),
            "report_context_evidence_scoreboard": final_state.get("report_context", {}).get(
                "evidence_scoreboard",
                {},
            ),
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "risky_history": final_state["risk_debate_state"]["risky_history"],
                "safe_history": final_state["risk_debate_state"]["safe_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
            "final_trade_intent": final_state.get("final_trade_intent", {}),
        }

        # Save to file
        safe_ticker = safe_ticker_component(self.ticker)
        directory = (
            Path(self.config.get("results_dir", "eval_results"))
            / safe_ticker
            / "TradingAgentsStrategy_logs"
        )
        directory.mkdir(parents=True, exist_ok=True)

        with open(directory / "full_states_log.json", "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict, f, indent=4)

    def reflect_and_remember(self, returns_losses):
        """Reflect on decisions and update memory based on returns."""
        self.reflector.reflect_bull_researcher(
            self.curr_state, returns_losses, self.bull_memory
        )
        self.reflector.reflect_bear_researcher(
            self.curr_state, returns_losses, self.bear_memory
        )
        self.reflector.reflect_trader(
            self.curr_state, returns_losses, self.trader_memory
        )
        self.reflector.reflect_invest_judge(
            self.curr_state, returns_losses, self.invest_judge_memory
        )
        self.reflector.reflect_risk_manager(
            self.curr_state, returns_losses, self.risk_manager_memory
        )

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
