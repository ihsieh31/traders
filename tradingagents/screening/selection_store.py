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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set

from tradingagents.screening.metrics import FACTOR_KEYS, FORMULA_VERSION
from tradingagents.screening.sessions import (
    current_trading_date,
    current_trading_date_production,
    eastern_now,
    is_fresh_utc_timestamp,
)

# Bumped to invalidate pre-remediation IEX-feed selections: R4 binds the
# consolidated feed ("sip") into the fingerprint and payload, and old
# schema-2 files (without data_feed) are never valid.
SCHEMA_VERSION = 3
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sealed = dict(payload)
        sealed[INTEGRITY_FIELD] = _integrity_digest(payload)
        text = json.dumps(sealed, indent=2, sort_keys=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".screening-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
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

        backend = config.get("screening_backend_url")
        if spec is not None:
            provider = spec.provider
            model = spec.model
            backend = spec.backend_url
        else:
            provider = config.get("screening_provider")
            model = config.get("screening_model")
        if provider == "local_openai" or (
            provider == "openai" and is_local_openai_enabled()
        ):
            from tradingagents.dataflows.config import get_openai_base_url

            backend = backend or get_openai_base_url()
        material = {
            "formula_version": FORMULA_VERSION,
            "schema_version": SCHEMA_VERSION,
            "provider": provider,
            "model": model,
            "backend_url": backend,
            "min_price": config.get("screening_min_price"),
            "min_adv20_usd": config.get("screening_min_adv20_usd"),
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
            if len(top20) != select_n or not (select_n <= len(top40) <= top_k * 10):
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


def default_selection_cache_path(config: Dict[str, Any]) -> str:
    configured = config.get("screening_selection_cache_path")
    if configured:
        return str(configured)
    cache_dir = config.get("data_cache_dir") or "dataflows/data_cache"
    return str(Path(cache_dir) / "screening_selection.json")


def eastern_timestamp(now=None) -> str:
    return eastern_now(now).isoformat()
