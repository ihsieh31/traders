"""Phase C daily Top20 selection cache.

One small JSON file (atomic replace, no second persistence system) holding
the trading date, the as_of session, generation metadata, the role/model,
a config fingerprint, the Top40 factor rows and the validated Top20. No
credentials are ever written.

Read-side discipline: a cached selection is ONLY usable after full
revalidation — schema, symbol membership, trading date, as_of/generation
timestamps (no future dates), and the config/provider/model fingerprint.
Next-day, corrupted, future-dated, or configuration-changed caches are
treated as absent (a trading-day round then rescans; a refresh failure
leaves the file removed so nothing can fall back to the stale list).

Integrity seal: ``save`` stamps every payload with ``integrity`` — a
SHA-256 over the canonical payload with the seal itself removed — and
``load_valid`` recomputes it before any other check, so an edited,
hand-crafted, or pre-seal file is treated as absent and the day rescans.
The seal is a self-hash, not an HMAC: it catches every edit that does not
also recompute the seal, but a writer who can rewrite the file can recompute
it too, so it is tamper *evidence* for operational consistency, not
provenance against a determined local attacker.

Cross-process first-scan safety: an advisory stdlib flock on a sibling
``.lock`` file serializes scan-and-replace so two concurrent runners
cannot both scan and interleave writes. The lock never wraps LLM analysis
of symbols and never wraps the broker execution lock.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from datetime import date
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set

from tradingagents.screening.metrics import FACTOR_KEYS, FORMULA_VERSION
from tradingagents.screening.quality import QUALITY_VERSION, quality_report_valid
from tradingagents.screening.sessions import (
    current_trading_date,
    current_trading_date_production,
    eastern_now,
    is_fresh_utc_timestamp,
)
from tradingagents.app_identity import validate_app_path

# Report gates, survivor scores and partial selections require a new schema;
# earlier selections must be rescanned even if their self-hash is intact.
SCHEMA_VERSION = 7
SCREENING_DATA_FEED = "sip"

# The integrity seal field name stamped by ``save`` and verified by
# ``load_valid`` before any other read-side check.
INTEGRITY_FIELD = "integrity"


def _integrity_digest(payload: dict) -> str:
    """SHA-256 over the canonical payload with the seal field removed."""
    material = {k: v for k, v in payload.items() if k != INTEGRITY_FIELD}
    canonical = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SelectionStore:
    """File-backed daily selection with strict read-side validation."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    # -- persistence -------------------------------------------------------

    def load_raw(self) -> Optional[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def save(self, payload: dict) -> None:
        sealed = dict(payload)
        sealed[INTEGRITY_FIELD] = _integrity_digest(payload)
        if isinstance(payload.get("data_quality"), dict):
            self._write_sealed(self.snapshot_path(payload), sealed)
        self._write_sealed(self.path, sealed)

    def snapshot_path(self, payload):
        day = date.fromisoformat(payload["trading_date"]).isoformat()
        return self.path.parent / "screening_snapshots" / f"{day}-{_integrity_digest(payload)}.json"

    @staticmethod
    def _write_sealed(path, sealed):
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(sealed, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".screening-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def invalidate(self) -> bool:
        """Remove the cached selection (manual-refresh failure semantics)."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    @contextmanager
    def scan_lock(self):
        """Serialize the scan-and-replace critical section across processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    # -- fingerprints ------------------------------------------------------

    @staticmethod
    def config_fingerprint(config: Dict[str, Any], spec: Any) -> str:
        """Hash of every configuration input that changes a selection."""
        from tradingagents.dataflows.config import is_local_openai_enabled
        from .policy import screening_method, ExclusionThresholds

        method = screening_method(config)

        backend = config.get("screening_backend_url")
        if spec is not None:
            provider = spec.provider
            model = spec.model
            backend = spec.backend_url
        else:
            provider = config.get("screening_provider")
            model = config.get("screening_model")
        if method == "exclusion":
            provider, model, backend = "deterministic", FORMULA_VERSION, None
        if provider == "local_openai" or (
            provider == "openai" and is_local_openai_enabled()
        ):
            from tradingagents.dataflows.config import get_openai_base_url

            backend = backend or get_openai_base_url()
        material = {
            "method": method,
            "exclusion_thresholds": vars(ExclusionThresholds.from_config(config)) if method == "exclusion" else None,
            "analysis_limit": config.get("screening_analysis_limit", 20),
            "screening_as_of_override": config.get("screening_as_of_override"),
            "formula_version": FORMULA_VERSION,
            "schema_version": SCHEMA_VERSION,
            "allow_shorts": bool(config.get("allow_shorts", False)),
            "provider": provider,
            "model": model,
            "backend_url": backend,
            "min_price": config.get("screening_min_price"),
            "min_adv20_usd": config.get("screening_min_adv20_usd"),
            "min_market_cap_usd": config.get("screening_min_market_cap_usd"),
            "required_bars": config.get("screening_required_bars"),
            "top_k": config.get("screening_top_k"),
            "select_n": config.get("screening_select_n"),
            "max_per_sector": config.get("screening_max_per_sector"),
            "bar_adjustment": config.get("screening_bar_adjustment"),
            "bars_batch_size": config.get("screening_bars_batch_size"),
            "sector_mapping": config.get("sector_mapping") or {},
            "max_sector_exposure_pct": config.get("max_sector_exposure_pct"),
            # R4: feed semantics change the selection; an IEX-era cache must
            # never validate as a SIP selection.
            "data_feed": SCREENING_DATA_FEED,
        }
        if SCHEMA_VERSION >= 7:
            material["quality_version"] = QUALITY_VERSION
        canonical = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # -- validation --------------------------------------------------------

    def load_valid(
        self,
        config: Dict[str, Any],
        *,
        spec: Any = None,
        now=None,
        calendar_client: Any = None,
        calendar_rows: Any = None,
    ) -> Optional[dict]:
        """Return the cached selection only if it fully revalidates.

        The cache's ``trading_date`` must equal the current authoritative
        trading date (on a non-trading day the authoritative date falls back
        to the last session, but callers must still refuse entries then).
        Calendar failures fail closed (treated as absent). Selections whose
        feed semantics are not the current consolidated ``sip`` feed are
        never valid.
        """
        payload = self.load_raw()
        if payload is None:
            return None

        # Integrity seal first: an edited, hand-crafted, or pre-seal file is
        # never usable, no matter how well it would otherwise validate.
        seal = payload.get(INTEGRITY_FIELD)
        if not isinstance(seal, str) or not seal:
            return None
        if seal != _integrity_digest(payload):
            return None

        select_n = int(config.get("screening_select_n", 20))
        top_k = int(config.get("screening_top_k", 40))
        try:
            if int(payload.get("schema_version", -1)) != SCHEMA_VERSION:
                return None
            top40 = payload.get("top40")
            top20 = payload.get("top20")
            if not isinstance(top40, list) or not isinstance(top20, list):
                return None
            if not selection_rows_valid(payload, config):
                return None

            trading_date = str(payload.get("trading_date", ""))
            as_of = str(payload.get("as_of", ""))
            if not is_fresh_utc_timestamp(str(payload.get("generated_at", "")), now=now):
                return None
            if not as_of or not trading_date:
                return None
            # R4: only consolidated-feed selections are usable.
            if payload.get("data_feed", "") != SCREENING_DATA_FEED:
                return None
            # Next-day, weekend, or backdated caches are never valid:
            # the trading_date must be the current authoritative trading date.
            try:
                injected = calendar_rows
                if injected is None and isinstance(config.get("calendar_rows"), list):
                    injected = config.get("calendar_rows")
                client = calendar_client
                if client is None and config.get("calendar_client") is not None:
                    client = config.get("calendar_client")
                expected_trading_date = str(
                    current_trading_date_production(now, client=client, calendar_rows=injected)
                )
            except Exception:
                return None
            if trading_date != expected_trading_date:
                return None
            if as_of > trading_date:
                return None
            from .metrics import resolve_as_of
            if as_of != str(resolve_as_of(config, now=now, calendar_client=client,
                                         calendar_rows=injected)):
                return None

            fingerprint = self.config_fingerprint(config, spec)
            if payload.get("config_fingerprint") != fingerprint:
                return None

            role = payload.get("role") or {}
            if spec is not None:
                if role.get("provider") != spec.provider or role.get("model") != spec.model:
                    return None

            top40_symbols = set()
            for row in top40:
                if not isinstance(row, dict) or not str(row.get("symbol", "")):
                    return None
                for key in ("symbol",) + FACTOR_KEYS:
                    if key not in row:
                        return None
                score = row.get("score")
                if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                    return None
                for key in ("positive_score", "negative_score"):
                    value = row.get(key)
                    if value is not None and (
                        not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or not 0.0 <= float(value) <= 100.0
                    ):
                        return None
                lane = row.get("candidate_lane")
                if lane is not None and lane not in (
                    "positive_trend", "negative_trend"
                ):
                    return None
                top40_symbols.add(str(row["symbol"]))

            seen_symbols: Set[str] = set()
            ranks: Set[int] = set()
            for index, entry in enumerate(top20, start=1):
                if not isinstance(entry, dict):
                    return None
                symbol = str(entry.get("symbol", ""))
                rank = entry.get("rank")
                reason = entry.get("short_reason", "")
                score = entry.get("screening_score")
                if symbol not in top40_symbols or symbol in seen_symbols:
                    return None
                if not isinstance(rank, int) or rank != index:
                    return None
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
                    return None
                if not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                    return None
                if not 0.0 <= float(score) <= 100.0:
                    return None
                seen_symbols.add(symbol)
                ranks.add(rank)

            sector_mode = payload.get("sector_mode")
            if not isinstance(sector_mode, dict) or "applied" not in sector_mode:
                return None
            if not isinstance(payload.get("stats"), dict):
                return None
        except (TypeError, ValueError):
            return None
        return payload

    @staticmethod
    def top20_symbols(selection: dict) -> list:
        return [entry["symbol"] for entry in selection.get("top20", [])]


def selection_rows_valid(payload, config):
    """Validate selection semantics for daily caches AND frozen A/B artifacts.

    Exclusion caches carry the full survivor pool (the legacy top40 field),
    allowing independent recomputation of percentiles and sector selection.
    """
    from .policy import screening_method, ExclusionThresholds
    from .metrics import (EligibilityThresholds, SymbolFeatures, score_exclusion_candidates,
                          select_exclusion_candidates)
    from .prompt import build_sector_plan

    try:
        method = screening_method(config)
        if payload.get("schema_version") != SCHEMA_VERSION or payload.get("data_feed") != SCREENING_DATA_FEED:
            return False
        if payload.get("method", "legacy") != method:
            return False
        if not quality_report_valid(payload, config):
            return False
        pool, selected = payload.get("top40"), payload.get("top20")
        n = config.get("screening_select_n", 20)
        if not isinstance(pool, list) or not isinstance(selected, list):
            return False
        if method == "legacy":
            if not 0 <= len(selected) <= min(n, len(pool)) or len(pool) > min(config.get("screening_top_k", 40), config.get("screening_analysis_limit", 20)):
                return False
        elif not 0 <= len(selected) <= n <= 20:
            return False
        symbols = [row["symbol"] for row in pool]
        chosen = [row["symbol"] for row in selected]
        if len(set(symbols)) != len(symbols) or len(set(chosen)) != len(chosen):
            return False
        if any(not isinstance(s, str) or not s or s != s.strip().upper() for s in symbols + chosen):
            return False
        if any(s not in symbols for s in chosen):
            return False
        for index, row in enumerate(selected, 1):
            score, rank, reason = row.get("screening_score"), row.get("rank"), row.get("short_reason")
            if (isinstance(rank, bool) or rank != index or isinstance(score, bool)
                    or not isinstance(score, (int, float)) or not math.isfinite(score)
                    or not 0 <= score <= 100 or not isinstance(reason, str)
                    or not 1 <= len(reason.strip()) <= 300):
                return False
        if method == "legacy":
            return True
        if payload.get("role") != {"provider": "deterministic", "model": FORMULA_VERSION, "endpoint": None}:
            return False
        features = []
        eligibility = EligibilityThresholds.from_config(config)
        for row in pool:
            values = {k: row[k] for k in ("price",) + FACTOR_KEYS}
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in values.values()):
                return False
            if values["price"] < eligibility.min_price or values["adv20"] < eligibility.min_adv20_usd:
                return False
            score = row.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
                return False
            if row.get("sector") != (config.get("sector_mapping") or {}).get(row["symbol"]):
                return False
            features.append(SymbolFeatures(symbol=row["symbol"], **values, score=row.get("score"),
                                           sector=row.get("sector")))
        ranked = score_exclusion_candidates(features, ExclusionThresholds.from_config(config))
        if len(ranked) != len(pool) or [f.symbol for f in ranked] != symbols:
            return False
        for row, item in zip(pool, ranked):
            score = row.get("exclusion_score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isclose(score, item.exclusion_score, abs_tol=1e-9):
                return False
        sector_plan = build_sector_plan(ranked, max_per_sector=config.get("screening_max_per_sector", 5), select_n=min(n, len(ranked)))
        recorded_sector = payload.get("sector_mode") or {}
        if any(recorded_sector.get(k) != sector_plan[k] for k in ("applied", "max_per_sector", "missing_sectors")):
            return False
        expected = select_exclusion_candidates(ranked, select_n=n, sector_plan=sector_plan)
        if chosen != [f.symbol for f in expected]:
            return False
        for index, (row, item) in enumerate(zip(selected, expected), 1):
            if row.get("rank") != index or row.get("screening_score") != item.exclusion_score:
                return False
            if any(row.get(k) != v for k, v in item.factor_row().items()):
                return False
            if not isinstance(row.get("short_reason"), str) or not 1 <= len(row["short_reason"].strip()) <= 300:
                return False
        stats = payload["stats"]
        return (len(pool) <= stats["eligible"] and len(pool) == stats["survivors"]
                and len(selected) == stats["selected"] and stats["selection_shortfall"] == n - len(selected))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def default_selection_cache_path(config: Dict[str, Any]) -> str:
    configured = config.get("screening_selection_cache_path")
    if configured:
        return str(validate_app_path(configured, field="screening_selection_cache_path"))
    cache_dir = config.get("data_cache_dir") or "dataflows/data_cache"
    return str(
        validate_app_path(cache_dir, field="data_cache_dir")
        / "screening_selection.json"
    )


def eastern_timestamp(now=None) -> str:
    return eastern_now(now).isoformat()
