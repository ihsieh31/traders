# TradingAgents/graph/signal_processing.py

import logging
import re

from langchain_openai import ChatOpenAI
from tradingagents.prompts import load_prompt


logger = logging.getLogger(__name__)

# Every value returned by process_signal must be one of these. The
# extraction LLM's response is untrusted free text: anything outside this
# set fails safe to the no-action signal instead of being persisted as a
# trade action.
ALLOWED_SIGNALS = frozenset({"LONG", "SHORT", "NEUTRAL", "BUY", "SELL", "HOLD"})
_NO_ACTION_SIGNAL = "HOLD"

_KEYWORD_RES = {
    action: re.compile(rf"\b{action}\b")
    for action in ("LONG", "SHORT", "NEUTRAL", "BUY", "SELL", "HOLD")
}


class SignalProcessor:
    """Processes trading signals to extract actionable decisions."""

    def __init__(self, quick_thinking_llm: ChatOpenAI):
        """Initialize with an LLM for processing."""
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """
        Process a full trading signal to extract the core decision.

        Args:
            full_signal: Complete trading signal text

        Returns:
            Extracted decision — one of ALLOWED_SIGNALS; unparseable output
            fails safe to the no-action signal (HOLD).
        """
        # First try deterministic extraction to avoid unnecessary LLM calls
        content = str(full_signal or "").upper()

        # Check for trading-mode keywords first (LONG / SHORT / NEUTRAL)
        for action in ("LONG", "SHORT", "NEUTRAL"):
            pattern = f"FINAL TRANSACTION PROPOSAL: **{action}**"
            if pattern in content:
                return action

        # Check for investment-mode keywords (BUY / SELL / HOLD)
        for action in ("BUY", "SELL", "HOLD"):
            pattern = f"FINAL TRANSACTION PROPOSAL: **{action}**"
            if pattern in content:
                return action

        # Fallback: standalone keyword search in the last 100 characters
        # (word-boundary only, so "BUYBACK" or "SHORTSELL" cannot match).
        tail = content[-100:]
        for action in ("LONG", "SHORT", "NEUTRAL", "BUY", "SELL", "HOLD"):
            if _KEYWORD_RES[action].search(tail):
                return action

        # If deterministic parsing fails, let the LLM infer. Its response is
        # untrusted free text (it may quote rationale or injected content):
        # only a cleaned, exact allowed action is accepted.
        messages = [
            (
                "system",
                load_prompt("graph/signal_extraction_system"),
            ),
            ("human", full_signal),
        ]

        raw = str(self.quick_thinking_llm.invoke(messages).content or "")
        candidate = re.sub(r"[^A-Z]", "", raw.upper())
        if candidate in ALLOWED_SIGNALS:
            return candidate
        # The extraction LLM may echo context around the action ("the
        # decision is BUY."). Accept the response only when exactly one
        # distinct allowed action appears; anything ambiguous or unknown
        # fails safe to the no-action signal.
        matches = {
            action
            for action in ALLOWED_SIGNALS
            if _KEYWORD_RES[action].search(raw.upper())
        }
        if len(matches) == 1:
            return matches.pop()
        logger.warning(
            "signal extraction produced a non-member value %r; failing safe to %s",
            raw,
            _NO_ACTION_SIGNAL,
        )
        return _NO_ACTION_SIGNAL
