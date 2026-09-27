import pandas as pd
import pytest

from tradingagents.backtest.engine import run_backtest
from tradingagents.backtest.metrics import annualized_return, max_drawdown, sharpe_ratio
from tradingagents.safety.guardrails import SafetyGuard
from tradingagents.analysis_backends.berkshire.calculations import verify_market_cap, cross_validate


@pytest.mark.parametrize("key,value", [
    ("max_trade_notional_usd", float("nan")),
    ("max_drawdown_halt_pct", float("inf")),
    ("daily_loss_halt_pct", -1), ("max_symbol_concentration_pct", True),
    ("daily_llm_token_budget", -1), ("max_consecutive_rejections", 1.5),
    ("safety_enabled", "not-a-boolean"),
])
def test_bad_safety_config_cannot_disable_caps(tmp_path, key, value):
    with pytest.raises(ValueError):
        SafetyGuard({key: value}, state_path=tmp_path / "state.json")


@pytest.mark.parametrize("tokens", [-1, True, 1.5])
def test_bad_token_update_never_corrupts_durable_budget(tmp_path, tokens):
    path = tmp_path / "state.json"
    guard = SafetyGuard(state_path=path)
    guard.record_llm_tokens(10, when="2026-09-26")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        guard.record_llm_tokens(tokens, when="2026-09-26")
    assert path.read_bytes() == before
    assert SafetyGuard(state_path=path).llm_tokens_used("2026-09-26") == 10


def test_safety_ledger_syncs_directory_metadata(monkeypatch, tmp_path):
    import os
    import stat

    calls = []
    original = os.fsync

    def fsync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    SafetyGuard(state_path=tmp_path / "safety.json").record_llm_tokens(1)
    assert False in calls  # file content
    assert True in calls  # committed rename survives power loss


@pytest.mark.parametrize("notional", [float("nan"), float("inf"), -1, True])
def test_invalid_required_notional_cannot_be_treated_as_zero(tmp_path, notional):
    verdict = SafetyGuard(state_path=tmp_path / "safety.json").check_order("AAPL", notional)
    assert not verdict.allowed
    assert "INVALID_ORDER_NOTIONAL" in verdict.reason_codes


def prices():
    return pd.DataFrame({"open": [100.] * 4, "high": [102.] * 4,
                         "low": [98.] * 4, "close": [100.] * 4},
                        index=pd.date_range("2026-09-21", periods=4))


@pytest.mark.parametrize("kwargs", [
    {"commission": -0.01}, {"slippage_bps": -5}, {"slippage_bps": float("nan")},
    {"initial_cash": 0}, {"initial_cash": float("inf")}, {"position_pct": 1.1},
    {"position_pct": -0.2}, {"periods_per_year": 0},
    {"slippage_model": "volatility", "slippage_min_bps": 10, "slippage_max_bps": 1},
    {"slippage_model": "volatility", "slippage_vol_fraction": -1},
])
def test_backtest_rejects_parameters_that_fabricate_economics(kwargs):
    with pytest.raises(ValueError):
        run_backtest(prices(), {}, **kwargs)


@pytest.mark.parametrize("signals", [{"2026-09-21": "TYPO"}, {"yesterday": "BUY"}])
def test_bad_backtest_signals_are_not_silently_treated_as_hold(signals):
    with pytest.raises(ValueError):
        run_backtest(prices(), signals)


def test_undefined_drawdown_and_overflowing_cagr_are_not_nan_or_infinite():
    assert max_drawdown([0, 0]) is None
    assert annualized_return([1, 1e100]) is None


@pytest.mark.parametrize("kwargs", [{"periods_per_year": 0}, {"risk_free_rate": float("nan")},
                                    {"risk_free_rate": -2}])
def test_invalid_sharpe_inputs_have_a_consistent_error(kwargs):
    with pytest.raises(ValueError):
        sharpe_ratio([100, 110, 108], **kwargs)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", -1])
def test_financial_calculations_reject_invalid_market_cap_inputs(value):
    with pytest.raises(ValueError):
        verify_market_cap(price=value, shares_outstanding=10)


def test_cross_validation_rejects_negative_tolerance():
    with pytest.raises(ValueError):
        cross_validate(primary=100, secondary=100, tolerance=-0.1)
