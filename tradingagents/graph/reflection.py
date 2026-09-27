
from tradingagents.redaction import sanitize_for_log
# TradingAgents/graph/reflection.py

from typing import Dict, Any
from langchain_openai import ChatOpenAI
from tradingagents.prompts import load_prompt, render_prompt


class Reflector:
    """Handles reflection on decisions and updating memory."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize the reflector with an LLM."""
        self.quick_thinking_llm = quick_thinking_llm
        self.reflection_system_prompt = self._get_reflection_prompt()

    def _get_reflection_prompt(self) -> str:
        """Get the system prompt for reflection."""
        return load_prompt("graph/reflection_system")

    def _extract_current_situation(self, current_state: Dict[str, Any]) -> str:
        """Extract the current market situation from the state."""
        curr_market_report = current_state["market_report"]
        curr_sentiment_report = current_state["sentiment_report"]
        curr_news_report = current_state["news_report"]
        curr_fundamentals_report = current_state["fundamentals_report"]

        return f"{curr_market_report}\n\n{curr_sentiment_report}\n\n{curr_news_report}\n\n{curr_fundamentals_report}"

    def _reflect_on_component(
        self, component_type: str, report: str, situation: str, returns_losses
    ) -> str:
        """Generate reflection for a component."""
        messages = [
            ("system", self.reflection_system_prompt),
            (
                "human",
                render_prompt(
                    "graph/reflection_component_human",
                    returns_losses=returns_losses,
                    report=report,
                    situation=situation,
                ),
            ),
        ]

        result = self.quick_thinking_llm.invoke(messages).content
        return result

    def reflect_bull_researcher(self, current_state, returns_losses, bull_memory):
        """Reflect on bull researcher's analysis and update memory."""
        situation = self._extract_current_situation(current_state)
        bull_debate_history = current_state["investment_debate_state"]["bull_history"]

        result = self._reflect_on_component(
            "BULL", bull_debate_history, situation, returns_losses
        )
        bull_memory.add_situations([(situation, result)])

    def reflect_bear_researcher(self, current_state, returns_losses, bear_memory):
        """Reflect on bear researcher's analysis and update memory."""
        situation = self._extract_current_situation(current_state)
        bear_debate_history = current_state["investment_debate_state"]["bear_history"]

        result = self._reflect_on_component(
            "BEAR", bear_debate_history, situation, returns_losses
        )
        bear_memory.add_situations([(situation, result)])

    def reflect_trader(self, current_state, returns_losses, trader_memory):
        """Reflect on trader's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        trader_decision = current_state["trader_investment_plan"]

        result = self._reflect_on_component(
            "TRADER", trader_decision, situation, returns_losses
        )
        trader_memory.add_situations([(situation, result)])

    def reflect_invest_judge(self, current_state, returns_losses, invest_judge_memory):
        """Reflect on investment judge's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        judge_decision = current_state["investment_debate_state"]["judge_decision"]

        result = self._reflect_on_component(
            "INVEST JUDGE", judge_decision, situation, returns_losses
        )
        invest_judge_memory.add_situations([(situation, result)])

    def reflect_risk_manager(self, current_state, returns_losses, risk_manager_memory):
        """Reflect on risk manager's decision and update memory."""
        situation = self._extract_current_situation(current_state)
        judge_decision = current_state["risk_debate_state"]["judge_decision"]

        result = self._reflect_on_component(
            "RISK JUDGE", judge_decision, situation, returns_losses
        )
        risk_manager_memory.add_situations([(situation, result)])

    def reflect_on_outcome(
        self,
        state: Dict[str, Any],
        returns_losses: str,
        memories: Dict[str, Any],
    ) -> Dict[str, bool]:
        """Run every per-agent reflection against a realized outcome.

        `state` is a final_state dict (live or recovered from a run log) and
        `memories` maps component name -> FinancialSituationMemory. Each
        component is isolated: one failure (missing key, LLM error) must not
        block the remaining lessons from being written.
        """
        reflectors = {
            "bull": self.reflect_bull_researcher,
            "bear": self.reflect_bear_researcher,
            "trader": self.reflect_trader,
            "invest_judge": self.reflect_invest_judge,
            "risk_manager": self.reflect_risk_manager,
        }
        results: Dict[str, bool] = {}
        for name, reflect in reflectors.items():
            memory = memories.get(name)
            if memory is None:
                continue
            try:
                reflect(state, returns_losses, memory)
                results[name] = True
            except Exception as exc:
                print(f"[REFLECTION] Skipped {name} reflection: {sanitize_for_log(str(exc))}")
                results[name] = False
        return results

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float | None,
    ) -> str:
        """Create a compact memory-log reflection for a completed final decision."""
        alpha_text = f"{alpha_return:+.1%}" if alpha_return is not None else "n/a"
        messages = [
            (
                "system",
                load_prompt("graph/reflection_final_memory_system"),
            ),
            (
                "human",
                render_prompt(
                    "graph/reflection_final_memory_human",
                    raw_return=f"{raw_return:+.1%}",
                    alpha_text=alpha_text,
                    final_decision=final_decision,
                ),
            ),
        ]
        result = self.quick_thinking_llm.invoke(messages)
        return result.content if hasattr(result, "content") else str(result)
