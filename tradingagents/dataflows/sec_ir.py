"""Phase B SEC / company-IR primary sources: minimal, honest, stdlib-only.

Supplements (never replaces) the existing market/news fallbacks. The
fundamentals analyst consumes the rendered report; a missing or stale
primary source is labeled as such and is never disguised as fresh data or
as a provider retry.

Scope and honesty boundaries:
- symbol -> CIK comes from the SEC's official company_tickers.json mapping
  (cached) or from test fixtures. No LLM ever guesses a CIK or URL.
- Filings: the official data.sec.gov submissions JSON for the resolved CIK;
  latest 10-K / 10-Q / 8-K only. No market-wide IR scanning.
- IR pages: only URLs explicitly configured per symbol; no entry is
  reported as missing, never guessed.
- Filing freshness uses the filing's published date (its annual/quarterly/
  event nature), NOT retrieved_at: a freshly retrieved year-old 10-K is a
  year-old filing. Missing/future/unparseable timestamps are never fresh.
- Transport: stdlib urllib with bounded timeout, bounded response size,
  official-host and redirect checks, a configurable SEC User-Agent, and
  request throttling. No scraping framework, no new dependencies.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlparse

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOCUMENT_URL_TEMPLATE = (
    "https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/{document}"
)
SEC_HOSTS = ("www.sec.gov", "www.sec.gov", "sec.gov", "data.sec.gov", "efts.sec.gov")

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2_000_000
SEC_THROTTLE_SECONDS = 0.12  # SEC fair-access policy: <= ~8 requests/second

# Filing freshness by the nature of the filing (implementation contract):
# 10-K annual (stale after ~16.5 months), 10-Q quarterly (~4.3 months),
# 8-K event-driven (~30 days), IR publication pages (~14 days).
DEFAULT_FRESHNESS_DAYS = {"10-K": 500, "10-Q": 130, "8-K": 30, "ir": 14}


class SecIrError(RuntimeError):
    """Raised when a primary source cannot be fetched or trusted."""


@dataclass(frozen=True)
class SourceRecord:
    """One primary-source record with honest metadata."""

    kind: str  # "sec_filing" | "ir_page"
    symbol: str
    source: str  # "SEC EDGAR" | "Company IR"
    url: str
    published_at: Optional[str]  # filing date / IR publication date (UTC ISO)
    retrieved_at: str  # fetch time (UTC ISO)
    form_type: Optional[str] = None
    title: str = ""
    excerpt: str = ""
    freshness_days: Optional[int] = None
    error: Optional[str] = None  # set when the source is unavailable

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def is_fresh(self) -> bool:
        if self.error or not self.published_at:
            return False
        try:
            published = datetime.fromisoformat(
                self.published_at.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if published.tzinfo is None:
            return False
        published = published.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if published > now + timedelta(minutes=5):
            return False  # future timestamps are never fresh
        days = self.freshness_days
        if days is None:
            return False
        return now - published <= timedelta(days=days)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecIrClient:
    """Official SEC filings and configured IR pages with bounded transport."""

    def __init__(
        self,
        *,
        cache_dir: Optional[str | Path] = None,
        user_agent: Optional[str] = None,
        ir_pages: Optional[Dict[str, str]] = None,
        freshness_days: Optional[Dict[str, int]] = None,
        fetch: Optional[Callable[[str, Dict[str, str], float], bytes]] = None,
        now: Callable[[], datetime] = datetime.now,
        throttle_seconds: float = SEC_THROTTLE_SECONDS,
    ):
        self.cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".tradingagents" / "cache" / "sec_ir"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = (
            user_agent
            or f"TradingAgents research contact via env SEC_IR_USER_AGENT ({_utc_now_iso()[:10]})"
        )
        self.ir_pages = {
            str(sym).upper().replace("/", ""): url for sym, url in (ir_pages or {}).items()
        }
        self.freshness = dict(DEFAULT_FRESHNESS_DAYS)
        self.freshness.update(freshness_days or {})
        self._external_fetch = fetch
        self._now = now
        self._throttle = throttle_seconds
        self._last_request_at = 0.0

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, *, expect_host: Optional[str] = None) -> tuple[bytes, Dict[str, str]]:
        """Bounded GET with official-host and redirect checks.

        Returns (body, response_headers)."""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise SecIrError(f"refusing non-https URL: {url.split('?')[0]}")
        if expect_host and parsed.hostname != expect_host:
            raise SecIrError(
                f"refusing cross-host redirect to {parsed.hostname}: "
                "only official hosts are trusted"
            )
        headers = {
            "User-Agent": self.user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": parsed.hostname or "",
        }
        if self._external_fetch is not None:
            result = self._external_fetch(url, headers, DEFAULT_TIMEOUT_SECONDS)
            if isinstance(result, tuple) and len(result) == 2:
                return result
            return result, {}

        # Throttle: SEC fair-access requires spacing between requests.
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._throttle:
            time.sleep(self._throttle - elapsed)
        self._last_request_at = time.monotonic()

        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as resp:
                final_host = urlparse(resp.geturl()).hostname
                if parsed.hostname != final_host and final_host not in SEC_HOSTS:
                    raise SecIrError(
                        f"non-official redirect target {final_host} is not trusted"
                    )
                data = resp.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {k: v for k, v in resp.headers.items()}
        except SecIrError:
            raise
        except urllib.error.HTTPError as exc:
            raise SecIrError(f"HTTP {exc.code} fetching {url.split('?')[0]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SecIrError(f"fetch failed for {url.split('?')[0]}: {exc}") from exc
        if len(data) > MAX_RESPONSE_BYTES:
            raise SecIrError(
                f"response from {url.split('?')[0]} exceeds the "
                f"{MAX_RESPONSE_BYTES} byte safety cap"
            )
        return data, response_headers

    # -- official CIK mapping -------------------------------------------------

    def resolve_cik(self, symbol: str) -> Optional[int]:
        """Resolve symbol -> CIK from the official SEC mapping. Never guesses."""
        normalized = (symbol or "").upper().replace("/", "")
        if not normalized or "/" in (symbol or ""):
            return None
        cache_path = self.cache_dir / "company_tickers.json"
        mapping = None
        try:
            stamp = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
            if datetime.now(timezone.utc) - stamp < timedelta(days=7):
                mapping = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapping = None
        if mapping is None:
            try:
                raw, _ = self._fetch(SEC_TICKERS_URL, expect_host="www.sec.gov")
                mapping = json.loads(raw.decode("utf-8"))
                cache_path.write_text(json.dumps(mapping), encoding="utf-8")
            except (SecIrError, ValueError) as exc:
                raise SecIrError(
                    f"official SEC ticker mapping unavailable for {normalized}: {exc}"
                ) from exc
        for entry in mapping.values():
            if str(entry.get("ticker", "")).upper() == normalized:
                return int(entry["cik_str"])
        return None

    # -- filings ---------------------------------------------------------------

    def latest_filings(self, symbol: str, forms: tuple = ("10-K", "10-Q", "8-K")) -> List[SourceRecord]:
        """Latest filings per form type from the official submissions feed."""
        normalized = (symbol or "").upper().replace("/", "")
        records: List[SourceRecord] = []
        try:
            cik = self.resolve_cik(normalized)
        except SecIrError as exc:
            return [
                SourceRecord(
                    kind="sec_filing", symbol=normalized, source="SEC EDGAR",
                    url=SEC_TICKERS_URL, published_at=None, retrieved_at=_utc_now_iso(),
                    form_type=form, error=str(exc),
                )
                for form in forms
            ]
        if cik is None:
            return [
                SourceRecord(
                    kind="sec_filing", symbol=normalized, source="SEC EDGAR",
                    url=SEC_TICKERS_URL, published_at=None, retrieved_at=_utc_now_iso(),
                    form_type=form,
                    error=f"{normalized} has no entry in the official SEC ticker mapping",
                )
                for form in forms
            ]
        try:
            raw, _ = self._fetch(
                SUBMISSIONS_URL_TEMPLATE.format(cik=cik), expect_host="data.sec.gov"
            )
            submissions = json.loads(raw.decode("utf-8"))
        except (SecIrError, ValueError) as exc:
            return [
                SourceRecord(
                    kind="sec_filing", symbol=normalized, source="SEC EDGAR",
                    url=SUBMISSIONS_URL_TEMPLATE.format(cik=cik), published_at=None,
                    retrieved_at=_utc_now_iso(), form_type=form, error=str(exc),
                )
                for form in forms
            ]

        recent = submissions.get("filings", {}).get("recent", {})
        form_list = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        documents = recent.get("primaryDocument", [])
        reports = recent.get("reportDate", [])
        seen_forms: set[str] = set()
        for idx, form in enumerate(form_list):
            if form not in forms or form in seen_forms:
                continue
            seen_forms.add(form)
            accession = accessions[idx] if idx < len(accessions) else ""
            document = documents[idx] if idx < len(documents) else ""
            filing_date = filing_dates[idx] if idx < len(filing_dates) else None
            report_date = reports[idx] if idx < len(reports) else None
            # published_at is the filing date (when it became public), not
            # the retrieval time. reportDate (period end) is context only.
            published_iso = (
                datetime.fromisoformat(filing_date).replace(tzinfo=timezone.utc).isoformat()
                if filing_date
                else None
            )
            accession_nodash = accession.replace("-", "") if accession else ""
            url = (
                DOCUMENT_URL_TEMPLATE.format(
                    cik=cik, accession_nodash=accession_nodash, document=document
                )
                if accession_nodash and document
                else SUBMISSIONS_URL_TEMPLATE.format(cik=cik)
            )
            excerpt = (
                f"Form {form} filed {filing_date or 'unknown'}"
                + (f" for period ending {report_date}" if report_date else "")
            )
            records.append(
                SourceRecord(
                    kind="sec_filing",
                    symbol=normalized,
                    source="SEC EDGAR",
                    url=url,
                    published_at=published_iso,
                    retrieved_at=_utc_now_iso(),
                    form_type=form,
                    title=f"{normalized} latest {form}",
                    excerpt=excerpt,
                    freshness_days=self.freshness.get(form),
                )
            )
        missing = [form for form in forms if form not in seen_forms]
        for form in missing:
            records.append(
                SourceRecord(
                    kind="sec_filing", symbol=normalized, source="SEC EDGAR",
                    url=SUBMISSIONS_URL_TEMPLATE.format(cik=cik), published_at=None,
                    retrieved_at=_utc_now_iso(), form_type=form,
                    error=f"no {form} found in the official submissions feed",
                )
            )
        return records

    # -- IR pages ----------------------------------------------------------------

    def ir_page(self, symbol: str) -> SourceRecord:
        """Fetch the configured IR publication page; missing URLs are reported,
        never guessed."""
        normalized = (symbol or "").upper().replace("/", "")
        url = self.ir_pages.get(normalized)
        if not url:
            return SourceRecord(
                kind="ir_page", symbol=normalized, source="Company IR", url="",
                published_at=None, retrieved_at=_utc_now_iso(),
                error=f"no IR page configured for {normalized} (company_ir_pages)",
            )
        host = urlparse(url).hostname
        try:
            body, response_headers = self._fetch(url, expect_host=host)
            text = body.decode("utf-8", errors="replace")
        except SecIrError as exc:
            return SourceRecord(
                kind="ir_page", symbol=normalized, source="Company IR", url=url,
                published_at=None, retrieved_at=_utc_now_iso(), error=str(exc),
            )
        # Publication date: HTTP Last-Modified when the site provides one;
        # otherwise unavailable (never fabricated from page content).
        last_modified = response_headers.get("Last-Modified")
        published_iso: Optional[str] = None
        if last_modified:
            from email.utils import parsedate_to_datetime

            try:
                published_iso = parsedate_to_datetime(last_modified).astimezone(
                    timezone.utc
                ).isoformat()
            except (TypeError, ValueError):
                published_iso = None
        title_start = text.find("<title>")
        title_end = text.find("</title>")
        title = (
            text[title_start + 7 : title_end].strip()
            if 0 <= title_start < title_end
            else ""
        )
        return SourceRecord(
            kind="ir_page",
            symbol=normalized,
            source="Company IR",
            url=url,
            published_at=published_iso,
            retrieved_at=_utc_now_iso(),
            title=title or f"{normalized} investor relations page",
            excerpt=text[:800],
            freshness_days=self.freshness.get("ir"),
        )


def render_sec_ir_report(
    symbol: str,
    records: List[SourceRecord],
    *,
    now: Optional[datetime] = None,
) -> str:
    """Render an honest primary-source block for the fundamentals analyst."""
    current = now or datetime.now(timezone.utc)
    lines = [
        f"Primary source check for {symbol} (SEC filings and configured "
        "company IR pages). Supplemental material only — the regular market "
        "and news data flow still applies and is labeled separately.",
    ]
    any_fresh = False
    for record in records:
        kind_label = "SEC filing" if record.kind == "sec_filing" else "Company IR page"
        if record.error:
            lines.append(
                f"- {kind_label} [{record.form_type or 'page'}]: UNAVAILABLE — "
                f"{record.error}"
            )
            continue
        try:
            published = datetime.fromisoformat(record.published_at.replace("Z", "+00:00"))
            age_days = (current - published).days
            published_text = f"{record.published_at} (~{age_days} days old)"
        except (ValueError, AttributeError, TypeError):
            published_text = "unavailable"
        if record.is_fresh:
            any_fresh = True
            freshness = "FRESH"
        else:
            freshness = (
                "STALE (published date is missing, future, or older than the "
                "configured freshness window for this filing type)"
            )
        lines.append(
            f"- {kind_label} [{record.form_type or 'page'}]: {freshness}; "
            f"published: {published_text}; retrieved: {record.retrieved_at}; "
            f"source: {record.source}; url: {record.url or 'unavailable'}"
        )
        if record.excerpt:
            excerpt = record.excerpt[:300]
            lines.append(f"  excerpt: {excerpt}")
    lines.append(
        "Retrieval time never upgrades a stale filing: a freshly fetched "
        "year-old 10-K is a year-old filing. Do not treat unavailable "
        "primary sources as evidence of routine data hiccups."
    )
    if not any_fresh:
        lines.append(
            "PRIMARY SOURCE STATUS: no fresh SEC/IR primary source available "
            "for this symbol. If the analysis depends on primary filings with "
            "no other usable data, the symbol must not produce a trading "
            "decision."
        )
    return "\n".join(lines)


def default_client_from_config(config: Optional[dict]) -> Optional[SecIrClient]:
    """Build a SecIrClient from runtime config; None when disabled."""
    if not config or not config.get("sec_ir_enabled", True):
        return None
    import os

    return SecIrClient(
        cache_dir=Path(config.get("data_cache_dir") or "eval_results") / "sec_ir",
        user_agent=os.getenv("SEC_IR_USER_AGENT") or config.get("sec_ir_user_agent"),
        ir_pages=dict(config.get("company_ir_pages") or {}),
        freshness_days=dict(config.get("sec_ir_freshness_days") or {}),
    )
