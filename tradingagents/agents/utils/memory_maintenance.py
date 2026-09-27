"""FinMem-style maintenance for the per-agent ChromaDB reflection memories.

Adapts the layered memory of FinMem (arXiv:2311.13743) to this project's
single-collection stores. FinMem retains events with an Ebbinghaus recency
score e^(-age/Q) whose stability constant Q depends on the layer (shallow
14d, intermediate 90d, deep 365d) and purges anything whose recency falls
below 0.05. With no discrete layers here, Q interpolates continuously with
an entry's importance — a lesson backed by a large realized move decays
like FinMem's deep layer, a trivial one like its shallow layer:

    importance = base + weight * min(|decision_return| / scale, 1)
    Q_eff      = Q_shallow + importance * (Q_deep - Q_shallow)
    recency    = exp(-age_days / Q_eff)

Age comes from the lesson's trade_date (batch-taught lessons enter with
their historical dates, exactly like FinMem's warm-up) or, failing that,
the created_at stamp add_situations now writes. Entries with neither are
given a neutral recency so pre-existing memories are never unfairly purged.

On top of decay, maintenance collapses near-duplicates (cosine similarity
above a threshold keeps only the highest-scoring copy) and enforces a
per-collection size cap by evicting the lowest-scoring entries first.
"""

from __future__ import annotations

from tradingagents.redaction import sanitize_for_log

import math
from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

import numpy as np


@dataclass
class MemoryMaintenanceConfig:
    max_entries: int = 500
    duplicate_similarity: float = 0.97
    recency_purge_threshold: float = 0.05
    q_shallow_days: float = 14.0  # FinMem shallow-layer stability
    q_deep_days: float = 365.0  # FinMem deep-layer stability
    importance_base: float = 0.3
    importance_weight: float = 0.7
    return_scale: float = 0.10  # |realized return| that maxes importance
    neutral_recency: float = 0.5  # for entries with no usable date

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "MemoryMaintenanceConfig":
        cfg = config or {}
        kwargs = {}
        mapping = {
            "max_entries": "memory_max_entries_per_collection",
            "duplicate_similarity": "memory_duplicate_similarity_threshold",
            "recency_purge_threshold": "memory_recency_purge_threshold",
        }
        for field_name, key in mapping.items():
            if cfg.get(key) is not None:
                kwargs[field_name] = cfg[key]
        return cls(**kwargs)


def entry_importance(
    metadata: Optional[dict], config: Optional[MemoryMaintenanceConfig] = None
) -> float:
    """Importance in [0,1]; lessons with larger realized moves matter more."""
    config = config or MemoryMaintenanceConfig()
    meta = metadata or {}
    try:
        realized = abs(float(meta["decision_return"]))
    except (KeyError, TypeError, ValueError):
        return config.importance_base
    scaled = min(realized / config.return_scale, 1.0) if config.return_scale else 1.0
    return min(config.importance_base + config.importance_weight * scaled, 1.0)


def _entry_age_days(metadata: Optional[dict]) -> Optional[float]:
    meta = metadata or {}
    for key in ("trade_date", "created_at"):
        raw = str(meta.get(key) or "")[:10]
        try:
            stamped = date.fromisoformat(raw)
        except ValueError:
            continue
        return max((date.today() - stamped).days, 0)
    return None


def entry_recency(
    metadata: Optional[dict],
    importance: float,
    config: Optional[MemoryMaintenanceConfig] = None,
) -> float:
    """Ebbinghaus decay e^(-age/Q) with Q interpolated by importance."""
    config = config or MemoryMaintenanceConfig()
    age = _entry_age_days(metadata)
    if age is None:
        return config.neutral_recency
    q_eff = config.q_shallow_days + importance * (
        config.q_deep_days - config.q_shallow_days
    )
    return math.exp(-age / q_eff) if q_eff > 0 else 0.0


def maintain_memory(
    memory, config: Optional[MemoryMaintenanceConfig] = None
) -> Dict[str, int]:
    """Purge expired entries, collapse near-duplicates, enforce the size cap.

    `memory` is a FinancialSituationMemory. Returns a summary of what was
    removed. Safe on empty collections.
    """
    config = config or MemoryMaintenanceConfig()
    collection = memory.situation_collection
    stored = collection.get(include=["metadatas", "embeddings"])
    ids = stored.get("ids") or []
    summary = {
        "kept": 0,
        "purged_expired": 0,
        "purged_duplicates": 0,
        "purged_over_cap": 0,
    }
    if not ids:
        return summary

    metadatas = stored.get("metadatas") or [{}] * len(ids)
    embeddings = stored.get("embeddings")

    entries = []
    for i, entry_id in enumerate(ids):
        meta = metadatas[i] or {}
        importance = entry_importance(meta, config)
        recency = entry_recency(meta, importance, config)
        embedding = None
        if embeddings is not None and len(embeddings) > i:
            embedding = np.asarray(embeddings[i], dtype=float)
        entries.append(
            {
                "id": entry_id,
                "score": recency + importance,
                "recency": recency,
                "embedding": embedding,
            }
        )

    to_delete = []

    # 1. FinMem forgetting rule: recency below the purge threshold.
    survivors = []
    for entry in entries:
        if entry["recency"] < config.recency_purge_threshold:
            to_delete.append(entry["id"])
            summary["purged_expired"] += 1
        else:
            survivors.append(entry)

    # 2. Near-duplicates: greedy keep-best by score, drop anything whose
    #    cosine similarity to an already-kept entry exceeds the threshold.
    survivors.sort(key=lambda e: e["score"], reverse=True)
    kept = []
    for entry in survivors:
        duplicate = False
        vec = entry["embedding"]
        if vec is not None and np.linalg.norm(vec) > 0:
            for other in kept:
                ovec = other["embedding"]
                if ovec is None:
                    continue
                denom = np.linalg.norm(vec) * np.linalg.norm(ovec)
                if denom <= 0:
                    continue
                if float(np.dot(vec, ovec)) / denom > config.duplicate_similarity:
                    duplicate = True
                    break
        if duplicate:
            to_delete.append(entry["id"])
            summary["purged_duplicates"] += 1
        else:
            kept.append(entry)

    # 3. Size cap: evict lowest-scoring entries (kept is sorted best-first).
    if config.max_entries and len(kept) > config.max_entries:
        for entry in kept[config.max_entries :]:
            to_delete.append(entry["id"])
            summary["purged_over_cap"] += 1
        kept = kept[: config.max_entries]

    if to_delete:
        collection.delete(ids=to_delete)
    summary["kept"] = len(kept)
    return summary


def maintain_all_memories(memories: Dict[str, object], config: Optional[dict] = None) -> Dict[str, dict]:
    """Run maintenance over a component->memory map, isolating failures."""
    maintenance_config = MemoryMaintenanceConfig.from_config(config)
    results: Dict[str, dict] = {}
    for name, memory in memories.items():
        if memory is None:
            continue
        try:
            results[name] = maintain_memory(memory, maintenance_config)
        except Exception as exc:
            print(f"[MEMORY] Maintenance skipped for {name}: {sanitize_for_log(str(exc))}")
    return results
