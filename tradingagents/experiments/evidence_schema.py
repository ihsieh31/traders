"""Typed shape for the shared, point-in-time evidence packet."""

from __future__ import annotations

from typing import Any, TypedDict


class EvidencePacket(TypedDict):
    schema_version: int
    symbol: str
    trade_date: str
    captured_at: str
    market: dict[str, Any]
    fundamentals: dict[str, Any]
    news: dict[str, Any]
    macro: dict[str, Any]
    social: dict[str, Any]
    sources: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    sha256: str
