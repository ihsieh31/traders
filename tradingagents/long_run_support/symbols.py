"""Per-symbol resume, analysis and execution state machine for one daily round.

The coordinator owns stop state and dependencies. This function shares the
journal by reference, preserves every persistence/checkpoint boundary and
returns the observed stop reason. It creates at most one analysis graph.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from tradingagents.app_identity import DEFAULT_RESULTS_DIR, validate_app_path


def _prepare_symbol_graph_config(
    graph_config: Dict[str, Any],
    runtime: Dict[str, Any],
    *,
    run_id: str,
    session_date: str,
    symbol: str,
    expected_sha256: Optional[str] = None,
):
    """Return ``(graph_config, evidence_identity)`` for one symbol.

    Traders shares the round-level graph config unchanged. Berkshire gets a
    per-symbol config bound to a deterministic frozen-evidence packet under
    the run's own isolated results namespace; capture/build reuses the shared
    A/B evidence module so hashing and integrity semantics are identical.

    Fail-closed: when the journal already pinned a packet hash, that hash is
    installed as the expected sha256 BEFORE build_or_load runs — a missing or
    tampered packet raises EvidenceIntegrityError instead of silently
    producing different evidence.
    """
    backend = str(runtime.get("analysis_backend") or "traders").strip().lower()
    if backend != "berkshire":
        return dict(graph_config), None

    from pathlib import Path

    from tradingagents.dataflows.utils import safe_ticker_component
    from tradingagents.experiments.evidence_snapshot import (
        build_or_load_evidence_packet,
    )

    config = dict(graph_config)
    config["analysis_backend"] = "berkshire"
    # Berkshire consumes the same five-analyst Traders topology prompts; the
    # profile key keeps that explicit in every audit/log record.
    config["analysis_profile"] = "traders"
    config["analysis_input_mode"] = "frozen_evidence"
    results_dir = validate_app_path(
        runtime.get("results_dir") or DEFAULT_RESULTS_DIR, field="results_dir"
    )
    packet_path = (
        Path(results_dir)
        / "_long_run_evidence"
        / safe_ticker_component(run_id)
        / safe_ticker_component(session_date)
        / safe_ticker_component(symbol)
        / "evidence_packet.json"
    )
    config["evidence_packet_path"] = str(packet_path)
    if expected_sha256:
        config["evidence_packet_sha256"] = str(expected_sha256)
    packet = build_or_load_evidence_packet(
        packet_path, symbol, trade_date=session_date, config=config
    )
    config["evidence_packet_sha256"] = packet["sha256"]
    return config, {"path": str(packet_path), "sha256": str(packet["sha256"])}


def run_symbol_work(
    *,
    journal,
    run_id,
    session_date,
    long_cfg,
    runtime,
    deps,
    service,
    ends_at,
    graph_factory,
    _recovery_can_submit,
    trade_intent_action,
    ProviderFailure,
    LongRunStop,
    _build_graph_config,
    _control_stop_reason,
    STOP_REASON_WINDOW_ENDED,
    STOP_REASON_STOP_REQUESTED,
    SYMBOL_PENDING,
    SYMBOL_ANALYZING,
    SYMBOL_ANALYZED,
    SYMBOL_EXECUTING,
    SYMBOL_DONE,
    SYMBOL_FAILED,
    save_round_journal,
    utc_now_iso,
    _execute_intent,
    _record_execution,
    _check_execution_hard_stop,
    _recover_intent_from_run_log,
    log_event,
    _normalize_intent,
) -> Optional[str]:
    graph_config = _build_graph_config(runtime, long_cfg, run_id=run_id)
    notional = float(long_cfg.get("base_trade_notional_usd") or 0)
    graph = None
    stop_reason: Optional[str] = None

    def _execute_symbol(symbol: str, intent: Dict[str, Any], *, _reentry: bool = False) -> None:
        # F10 checkpoint 6: immediately before broker execution. A stop that
        # became observable while the analysis ran must prevent the order.
        control = _control_stop_reason(deps, ends_at)
        if control:
            nonlocal stop_reason
            if control == STOP_REASON_WINDOW_ENDED and not _reentry:
                journal["symbols"][symbol]["execution_result_summary"] = {
                    "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                }
                journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                journal["symbols"][symbol]["status"] = SYMBOL_DONE  # analyzed, not traded
                save_round_journal(run_id, journal)
            stop_reason = control
            return
        journal["symbols"][symbol]["status"] = SYMBOL_EXECUTING
        save_round_journal(run_id, journal)
        try:
            result = _execute_intent(
                deps, service, symbol, intent, notional,
                run_id=run_id, session_date=session_date,
                allow_shorts=bool(runtime.get("allow_shorts", False)),
                can_submit=_recovery_can_submit,
            )
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop("EXECUTION_AMBIGUOUS",
                              f"{symbol}: execution failed: {exc}")
        _record_execution(journal, run_id, symbol, result)
        _check_execution_hard_stop(symbol, result)

    for symbol in list(journal["symbols"]):
        entry = journal["symbols"][symbol]
        status = entry.get("status")

        if status == SYMBOL_DONE:
            continue  # Case E: never analyze or execute twice for one session.
        if status == SYMBOL_FAILED:
            continue

        # F10 checkpoint 2: before starting each symbol's work.
        control = _control_stop_reason(deps, ends_at)
        if control:
            stop_reason = control
            break

        # Case D (resume): re-enter execution with the identical decision
        # identity; the durable outbox dedupes without a second broker POST.
        if status == SYMBOL_EXECUTING:
            # F10 checkpoint 5: before re-entering EXECUTING recovery.
            control = _control_stop_reason(deps, ends_at)
            if control:
                stop_reason = control
                break
            intent = entry.get("trade_intent")
            if not intent:
                entry["status"] = SYMBOL_FAILED
                entry["execution_result_summary"] = {"error": "EXECUTING without intent"}
                save_round_journal(run_id, journal)
                continue
            try:
                recovery = service.startup_recover(can_submit=_recovery_can_submit)
                if not recovery.get("success"):
                    raise LongRunStop("RECOVERY_UNSAFE",
                                      f"re-entry recovery unsafe: {recovery.get('reconciliation_reasons')}")
                result = _execute_intent(
                    deps, service, symbol, intent, notional,
                    run_id=run_id, session_date=session_date,
                    allow_shorts=bool(runtime.get("allow_shorts", False)),
                    can_submit=_recovery_can_submit,
                )
            except LongRunStop:
                raise
            except Exception as exc:
                raise LongRunStop("EXECUTION_AMBIGUOUS",
                                  f"{symbol}: re-entry failed: {exc}")
            _record_execution(journal, run_id, symbol, result)
            _check_execution_hard_stop(symbol, result)
            continue

        # Case C: reuse the exact persisted intent, never re-ask the LLM.
        if status == SYMBOL_ANALYZED:
            intent = entry.get("trade_intent")
            if not intent:
                entry["status"] = SYMBOL_PENDING
            else:
                # F10 checkpoint 4: a persisted ANALYZED intent must be
                # re-checked before executing on resume.
                control = _control_stop_reason(deps, ends_at)
                if control:
                    if control == STOP_REASON_WINDOW_ENDED:
                        entry["execution_result_summary"] = {
                            "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                        }
                        entry["status"] = SYMBOL_DONE
                        journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                        save_round_journal(run_id, journal)
                        stop_reason = STOP_REASON_WINDOW_ENDED
                        break
                    stop_reason = control
                    break
                _execute_symbol(symbol, intent)
                continue

        # Case B: ANALYZING means the previous process died before writing
        # ANALYZED. Broker execution is forbidden before ANALYZED, so reuse a
        # provably completed run — of THIS observation only (F12) — or safely
        # re-run the analysis.
        if status == SYMBOL_ANALYZING:
            recovered = _recover_intent_from_run_log(
                symbol, session_date,
                observation_id=run_id,
                results_dir=str(validate_app_path(
                    runtime.get("results_dir") or DEFAULT_RESULTS_DIR,
                    field="results_dir",
                )),
            )
            if recovered:
                entry["trade_intent"] = recovered
                entry["signal"] = trade_intent_action(recovered)
                entry["status"] = SYMBOL_ANALYZED
                save_round_journal(run_id, journal)
                log_event(run_id, "symbol_recovered",
                          {"symbol": symbol, "signal": entry["signal"]})
                _execute_symbol(symbol, recovered)
                continue
            entry["status"] = SYMBOL_PENDING

        # Case A: analyze normally (PENDING, or ANALYZING without proof).
        if entry.get("status") != SYMBOL_PENDING:
            continue
        # F19 checkpoint: earlier symbols may have consumed the rest of the
        # day's budget, so every fresh analysis re-checks before new LLM work.
        try:
            from tradingagents.safety import get_safety_guard

            verdict = get_safety_guard().check_llm_budget()
            if not verdict.allowed:
                raise LongRunStop(
                    "LLM_BUDGET_EXHAUSTED",
                    f"{symbol}: {'; '.join(verdict.reasons)}",
                )
        except LongRunStop:
            raise
        except Exception as exc:
            raise LongRunStop(
                "LLM_BUDGET_EXHAUSTED", f"budget check unavailable: {exc}"
            )
        entry["status"] = SYMBOL_ANALYZING
        symbol_started = utc_now_iso()
        # Persist the analysis-start marker BEFORE propagate so a crash leaves
        # an explicit start identity/time. It alone never claims completion.
        entry["analysis_run_ref"] = symbol_started
        save_round_journal(run_id, journal)
        try:
            backend = str(runtime.get("analysis_backend") or "traders").strip().lower()
            if backend == "berkshire":
                # Berkshire: per-symbol frozen evidence + a dedicated graph.
                # The packet identity is pinned into the journal BEFORE the
                # graph runs; a resume re-verifies the same bytes and any
                # tamper/missing packet fails closed in build_or_load.
                symbol_config, evidence = _prepare_symbol_graph_config(
                    graph_config, runtime,
                    run_id=run_id, session_date=session_date, symbol=symbol,
                    expected_sha256=entry.get("evidence_packet_sha256"),
                )
                entry["evidence_packet_path"] = evidence["path"]
                entry["evidence_packet_sha256"] = evidence["sha256"]
                save_round_journal(run_id, journal)
                graph = graph_factory(symbol_config)
            else:
                # F07: the Traders graph is shared across the round's symbols;
                # created lazily once, never per symbol.
                if graph is None:
                    graph = graph_factory(graph_config)
            final_state, _signal = graph.propagate(symbol, session_date)
            # F10 checkpoint 3: immediately after analysis returns, BEFORE
            # persisting a tradeable intent or starting broker execution.
            control = _control_stop_reason(deps, ends_at)
            if control:
                if control == STOP_REASON_WINDOW_ENDED:
                    # Keep the analysis result for audit, but never trade it:
                    # the authorized window ended while this symbol ran.
                    entry["trade_intent"] = _normalize_intent(
                        (final_state or {}).get("final_trade_intent")
                    )
                    entry["signal"] = None
                    entry["execution_result_summary"] = {
                        "no_trade": True, "error": "WINDOW_ENDED_DURING_ROUND",
                    }
                    journal["stop_reason"] = "WINDOW_ENDED_DURING_ROUND"
                    save_round_journal(run_id, journal)
                    log_event(run_id, "symbol_window_ended", {"symbol": symbol})
                    stop_reason = STOP_REASON_WINDOW_ENDED
                    break
                # STOP_REQUESTED: persist resume-safe journal state and yield
                # to the outer loop without marking remaining symbols FAILED.
                save_round_journal(run_id, journal)
                stop_reason = STOP_REASON_STOP_REQUESTED
                break
            intent = _normalize_intent((final_state or {}).get("final_trade_intent"))
            if not intent:
                from tradingagents.execution.service import validate_trade_intent

                _, err = validate_trade_intent(None)
                entry["status"] = SYMBOL_DONE  # fail closed: completed, no trade.
                entry["signal"] = None
                entry["execution_result_summary"] = {
                    "no_trade": True, "error": f"no schema-valid TradeIntent ({err})",
                }
                save_round_journal(run_id, journal)
                log_event(run_id, "symbol_no_intent", {"symbol": symbol})
                continue
            entry["trade_intent"] = intent
            entry["signal"] = trade_intent_action(intent)
            entry["status"] = SYMBOL_ANALYZED
            entry["analysis_run_ref"] = symbol_started
            save_round_journal(run_id, journal)
            log_event(run_id, "symbol_analyzed",
                      {"symbol": symbol, "signal": entry["signal"]})
        except ProviderFailure as exc:
            entry["status"] = SYMBOL_FAILED
            save_round_journal(run_id, journal)
            raise LongRunStop("PROVIDER_FAILURE", f"{symbol}: {exc}")
        except LongRunStop:
            raise
        except Exception as exc:
            entry["status"] = SYMBOL_FAILED
            entry["execution_result_summary"] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]
            }
            save_round_journal(run_id, journal)
            log_event(run_id, "symbol_failed",
                      {"symbol": symbol, "error": str(exc)[:200]})
            continue

        # Fresh ANALYZED → execute immediately (same pass, no re-loop needed).
        _execute_symbol(symbol, entry["trade_intent"])
        if stop_reason:
            break

    return stop_reason
