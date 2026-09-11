"""Economic/decision consistency regressions. No network, no live account."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import numpy as np
import pandas as pd
import pytest

from tradingagents.agents.schemas import RiskDecision, EntryPolicy, build_trade_intent_from_risk_decision, extract_protective_price
from tradingagents.execution.policy import entry_check
from tradingagents.execution.service import ExecutionService
from tradingagents.execution.lifecycle import due_positions
from tradingagents.execution.store import ExecutionStore
from tradingagents.dataflows.technical_brief import _adx, _rsi, compute_indicators, detect_trend
from tradingagents.agents.utils.report_context import _score_freshness
from tradingagents.backtest.teach import compute_decision_outcomes, _deterministic_lesson
from tradingagents.backtest.signals import load_recorded_signals
from tradingagents.backtest.engine import run_backtest, run_walk_forward
from tradingagents.risk.position_sizing import PositionSizer

NOW = datetime(2026, 9, 7, 14, tzinfo=timezone.utc)


def intent(now=NOW):
    decision = RiskDecision(action="BUY", confidence="high", risk_rationale="test", required_controls="stop",
                            stop_loss_price=90, take_profit_price=120, entry_guidance="only in range", time_horizon="5 days",
                            entry_policy=EntryPolicy(status="READY", minimum_price=99, maximum_price=101,
                                expires_at=(now + timedelta(hours=1)).isoformat(),
                                exit_by=(now + timedelta(days=5)).isoformat(),
                                risk_fraction=.01, confirmation="observed setup"))
    result = build_trade_intent_from_risk_decision(symbol="AAPL", trading_mode="investment", current_position="NEUTRAL", decision=decision)
    result.generated_at = now.isoformat()
    return result.model_dump(mode="json")


def prices(n=15):
    return pd.DataFrame({"timestamp": pd.bdate_range("2026-01-05", periods=n),
                         "open": np.arange(n) + 100., "high": np.arange(n) + 102.,
                         "low": np.arange(n) + 98., "close": np.arange(n) + 100., "volume": 1000})


def test_entry_preserves_conditions_and_bounds_actual_stop_risk():
    data = intent()
    assert data["entry_guidance"] == "only in range"
    assert data["time_horizon"] == "5 days"
    amount, reason = entry_check(data, now=NOW, price=100., equity=100000, requested=90000)
    assert reason is None
    assert amount * (101 - 90) / 101 <= 1000


@pytest.mark.parametrize("change", [
    {"status": "WAIT"}, {"confirmation": ""}, {"expires_at": NOW.isoformat()},
    {"expires_at": "2026-09-07T15:00:00"}, {"maximum_price": 98}, {"risk_fraction": .04},
    {"exit_by": (NOW+timedelta(days=40)).isoformat()}, {"maximum_notional": -1},
])
def test_bad_policy_never_authorizes_exposure(change):
    data = intent(); data["entry_policy"].update(change)
    assert entry_check(data, now=NOW, price=100., equity=100000, requested=1000)[1]


def test_unconfirmed_legacy_open_never_contacts_broker(tmp_path):
    broker = Mock(side_effect=AssertionError("must not contact broker"))
    service = ExecutionService(db_path=tmp_path/'orders.db', broker_factory=broker)
    data = intent(); data.pop("entry_policy")
    result = service.execute(trade_intent=data, dollar_amount=1000)
    assert result["entry_policy_blocked"] and not result["success"]
    broker.assert_not_called()


@pytest.mark.parametrize("price", [98., 102., float('nan')])
def test_quote_must_still_be_inside_entry_range(price):
    assert entry_check(intent(), now=NOW, price=price, equity=100000, requested=1000)[1]


@pytest.mark.parametrize("text", ["2 ATR below $100", "8% below entry", "$90 to $95", "below support", "2026-09-07"])
def test_protective_parser_never_guesses(text):
    assert extract_protective_price(text) is None


def test_absolute_price_and_no_assumed_kelly_edge():
    assert extract_protective_price('$1,250.50') == 1250.5
    values = [PositionSizer().size_position(equity=100000, price=100, atr=2, confidence=c,
               requested_notional=10000, stop_loss_price=90).notional for c in ('high','low')]
    assert values == [10000.,10000.]


def test_adx_outside_equal_moves_have_no_direction():
    # Both sides expand equally: Wilder assigns neither side any DM.
    n=80
    assert _adx(pd.Series(100.+np.arange(n)), pd.Series(90.-np.arange(n)), pd.Series([95.]*n)).iloc[-1] == 0
    assert _rsi(pd.Series(np.arange(50.)+100)).iloc[-1] == 100


def test_daily_sma200_has_enough_warmup_and_missing_is_null():
    with patch('tradingagents.dataflows.technical_brief.AlpacaUtils.get_stock_data', return_value=prices(230)) as fetch:
        frame = compute_indicators('AAPL', '2026-09-07', '1d')
    assert (pd.Timestamp('2026-09-07')-pd.Timestamp(fetch.call_args.kwargs['start_date'])).days >= 365
    assert detect_trend(frame).sma_200 is not None
    assert detect_trend(frame.iloc[:60]).sma_200 is None


def test_freshness_never_invents_publication_date():
    assert _score_freshness('latest EPS 4.20', NOW)[0] == 0
    assert _score_freshness('earnings on 2026-10-01', NOW)[0] == 0


def test_labels_preserve_position_and_do_not_teach_partial_horizon():
    signals={'2026-01-05':'SELL','2026-01-06':'SHORT','2026-01-07':'HOLD','2026-01-08':'HOLD'}
    results=compute_decision_outcomes(prices(),signals,3,positions={'2026-01-07':'LONG'})
    assert results[0]['decision_return']==0
    assert results[1]['decision_return']<0
    assert results[2]['decision_return']>0
    assert results[3]['decision_return'] is None
    assert 'not broker realized P&L' in _deterministic_lesson('AAPL',results[2])
    assert compute_decision_outcomes(prices(4),{'2026-01-05':'BUY'},5)==[]


def test_neutral_flattens_short_and_hold_retains_it():
    signals={'2026-01-05':'SHORT','2026-01-07':'NEUTRAL'}
    result=run_backtest(prices(10),signals,allow_shorts=True,commission=0,slippage_bps=0)
    assert len(result.trade_pnls)==1
    assert result.orders[0]['side']=='sell' and result.orders[1]['side']=='buy'
    assert result.to_dict()['evaluation_scope']=='single_symbol_signal_diagnostic'


def test_window_does_not_reexecute_earlier_signals():
    result=run_walk_forward(prices(15),{'2026-01-05':'BUY'},window_bars=5)
    assert result.windows[1]['metrics']['cumulative_return']==0


def write_forward(root, day, action, started=None, summary=None):
    folder=root/'AAPL'/'TradingAgentsStrategy_logs'/'runs';folder.mkdir(parents=True,exist_ok=True)
    stamp=started or day+'T15:00:00+00:00'
    payload={'symbol':'AAPL','trade_date':day,'status':'completed','started_at':stamp,'ended_at':stamp,
             'summary':summary or {'final_signal':action,'llm_call_events':1,'tool_events':1}}
    (folder/(stamp.replace(':','')+action+'.json')).write_text(json.dumps(payload))


def test_forward_loader_rejects_fixtures_and_hindsight_and_keeps_first(tmp_path):
    write_forward(tmp_path,'2026-01-05','BUY')
    write_forward(tmp_path,'2026-01-05','SELL','2026-01-05T16:00:00+00:00')
    write_forward(tmp_path,'2026-01-06','SELL','2026-09-07T16:00:00+00:00')
    write_forward(tmp_path,'2026-01-07','BUY',summary={'final_signal':'BUY'})
    assert load_recorded_signals('AAPL',str(tmp_path))=={'2026-01-05':'BUY'}


def test_memory_requires_opt_in_and_point_in_time_cutoff():
    from tradingagents.agents.utils.memory import FinancialSituationMemory
    memory=object.__new__(FinancialSituationMemory)
    memory.embeddings_enabled=True;memory.retrieval_enabled=False
    memory.get_embedding=Mock(side_effect=AssertionError('disabled'))
    assert memory.get_memories('test',as_of='2026-09-07')==[]
    memory.retrieval_enabled=True;memory.get_embedding=Mock(return_value=[0.,1.])
    memory.situation_collection=Mock()
    memory.situation_collection.query.return_value={'documents':[[]],'metadatas':[[]],'distances':[[]]}
    memory.get_memories('test',as_of='2026-09-07')
    assert memory.situation_collection.query.call_args.kwargs['where']['available_at_epoch']['$lt']==datetime(2026,9,7,tzinfo=timezone.utc).timestamp()


def test_lifecycle_uses_remaining_filled_lots_not_closed_old_positions(tmp_path):
    store=ExecutionStore(tmp_path/'orders.db')
    def fill(did,side,qty,when,policy):
        _,orders,_=store.create_outbox(decision_id=did,run_id=None,symbol='AAPL',action='BUY' if side=='buy' else 'SELL',target_position='LONG' if side=='buy' else 'NEUTRAL',payload_json=json.dumps({'entry_policy':policy}),orders=[{'client_order_id':did,'symbol':'AAPL','side':side,'quantity':qty,'notional':None}])
        store.record_fill(execution_id=did,order_id=orders[0]['order_id'],qty=qty,price=100,filled_at=when)
    fill('open','buy',10,'2026-09-01T14:00:00+00:00',{'exit_by':'2026-09-03T14:00:00+00:00'})
    fill('close','sell',10,'2026-09-02T14:00:00+00:00',{})
    fill('new','buy',5,'2026-09-04T14:00:00+00:00',{'exit_by':'2026-09-09T14:00:00+00:00'})
    assert due_positions(store,NOW)=={}
    assert due_positions(store,NOW+timedelta(days=3))['AAPL']['qty']==5


class PaperBrokerFixture:
    """In-memory fills and native OTO children; never contacts Alpaca."""
    def __init__(self):
        self.orders = []
        self.qty = 0
        self.submits = []
        self.cancels = []

    def get_clock(self):
        # R13: the opening gate proves the regular session from the broker's
        # own clock before any exposure-adding POST; the fixture keeps it open.
        return SimpleNamespace(is_open=True)

    def get_account(self):
        return SimpleNamespace(id='test-account', equity='100000', last_equity='100000', cash='100000', buying_power='100000')

    def get_all_positions(self):
        return [SimpleNamespace(symbol='AAPL', qty=self.qty, market_value=self.qty*100)] if self.qty else []

    def get_orders(self, request):
        return self.orders

    def get_order_by_id(self, order_id, filter=None):
        return next(o for o in self.orders if o.id == order_id)

    def submit_order(self, request):
        self.submits.append(request)
        qty = float(request.qty)
        side = request.side.value
        self.qty += qty if side == 'buy' else -qty
        stamp = datetime.now(timezone.utc)
        order = SimpleNamespace(id=f'broker-{len(self.submits)}', client_order_id=request.client_order_id,
                                symbol='AAPL', side=side, qty=qty, filled_qty=qty,
                                filled_avg_price=100, status='filled', updated_at=stamp, legs=[], notional=None)
        self.orders.append(order)
        if request.stop_loss:
            child = SimpleNamespace(id=f'child-{len(self.submits)}', client_order_id=f'broker-stop-{len(self.submits)}',
                                    symbol='AAPL', side='sell' if side == 'buy' else 'buy', qty=qty, filled_qty=0,
                                    filled_avg_price=None, status='accepted', updated_at=stamp, legs=[], notional=None)
            order.legs = [child]
            self.orders.append(child)
        return order

    def cancel_order_by_id(self, order_id):
        self.cancels.append(order_id)
        self.get_order_by_id(order_id).status = 'canceled'


def live_fixture(tmp_path):
    from tradingagents.execution.authority import BrokerQuote
    broker = PaperBrokerFixture()
    service = ExecutionService(db_path=tmp_path/'execution.db', broker_factory=lambda: broker,
        quote_factory=lambda s: BrokerQuote('AAPL', 99.9, 100.1, datetime.now(timezone.utc)))
    # Keep the fixture independent of saved user screening/quarantine settings.
    service._quarantine_rejection = Mock(return_value=None)
    return service, broker


def test_protected_entry_stop_fill_updates_lots_without_duplicate_exit(tmp_path):
    service, broker = live_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        result = service.execute(trade_intent=intent(now), dollar_amount=10000)
        assert result['success'], result
        assert not result.get('paused'), result
        assert broker.submits[0].qty * (101-90) <= 1000
        child = broker.orders[1]
        child.status = 'filled'; child.filled_qty = child.qty; child.filled_avg_price = 90
        child.updated_at = datetime.now(timezone.utc)
        broker.qty = 0
        recovered = service.startup_recover()
    assert recovered['success'], recovered
    assert due_positions(service.store, now + timedelta(days=6)) == {}
    assert len(service.store.list_fills_since('')) == 2
    assert len(broker.submits) == 1


def test_due_exit_cancels_owned_stop_then_closes_once(tmp_path):
    service, broker = live_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        assert service.execute(trade_intent=intent(now), dollar_amount=1000)['success']
        with patch('tradingagents.execution.service.utc_now', return_value=now+timedelta(days=6)):
            result = service.enforce_exit_deadlines()
            again = service.enforce_exit_deadlines()
    assert result['success'], result
    assert len(result['deadline_exits']) == 1
    assert result['broker_calls'] == 2  # cancel protection + close
    assert broker.qty == 0 and len(broker.submits) == 2
    assert broker.cancels == ['child-1']
    assert again['deadline_exits'] == []


def test_kill_switch_prevents_deadline_cancellation_and_close(tmp_path):
    service, broker = live_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        assert service.execute(trade_intent=intent(now), dollar_amount=1000)['success']
    guard = Mock(enabled=True)
    guard.check_order.return_value = SimpleNamespace(allowed=False, reasons=['kill switch'])
    with patch('tradingagents.safety.get_safety_guard', return_value=guard), \
         patch('tradingagents.execution.service.utc_now', return_value=now+timedelta(days=6)):
        result = service.enforce_exit_deadlines()
    assert not result['success'] and 'kill switch' in result['error']
    assert len(broker.submits) == 1 and not broker.cancels


def test_explicit_sell_cancels_owned_stop_and_closes_protected_position(tmp_path):
    service, broker = live_fixture(tmp_path)
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        assert service.execute(trade_intent=intent(datetime.now(timezone.utc)), dollar_amount=1000)['success']
        close = build_trade_intent_from_risk_decision(symbol='AAPL', trading_mode='investment',
                    current_position='LONG', decision=RiskDecision(action='SELL', confidence='high',
                    risk_rationale='exit', required_controls='close existing position'))
        result = service.execute(trade_intent=close.model_dump(mode='json'))
    assert result['success'], result
    assert not result.get('paused'), result
    assert broker.qty == 0 and broker.cancels == ['child-1']
    assert result['broker_calls'] == 2


def test_pending_protection_cancellation_never_allows_duplicate_close(tmp_path):
    service, broker = live_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    def pending(order_id):
        broker.get_order_by_id(order_id).status = 'pending_cancel'
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        assert service.execute(trade_intent=intent(now), dollar_amount=1000)['success']
        broker.cancel_order_by_id = pending
        with patch('tradingagents.execution.service.utc_now', return_value=now+timedelta(days=6)):
            result = service.enforce_exit_deadlines()
    assert not result['success'], result
    assert len(broker.submits) == 1 and broker.qty > 0


def test_audit_log_flush_stays_in_configured_directory(tmp_path, monkeypatch):
    from tradingagents.run_logger import RunAuditLogger
    monkeypatch.chdir(tmp_path)
    root = tmp_path/'isolated-results'
    logger = RunAuditLogger()
    run = logger.start_run('AAPL', '2026-09-07', config={'results_dir': str(root)}, metadata={'evaluation_mode': 'fixture'})
    logger.finish_run(run_id=run, status='completed')
    files = list(root.glob('AAPL/TradingAgentsStrategy_logs/runs/*.json'))
    assert len(files) == 1 and json.loads(files[0].read_text())['status'] == 'completed'
    assert not (tmp_path/'eval_results').exists()
    assert load_recorded_signals('AAPL', str(root)) == {}


def test_fred_requests_the_vintage_known_on_analysis_date():
    from tradingagents.dataflows.macro_utils import get_fred_data
    response = Mock(); response.json.return_value = {'observations': []}
    with patch('tradingagents.dataflows.macro_utils.get_fred_api_key', return_value='fixture'), \
         patch('tradingagents.dataflows.macro_utils.requests.get', return_value=response) as fetch:
        get_fred_data('CPIAUCSL', '2025-01-01', '2025-03-01')
    assert fetch.call_args.kwargs['params']['realtime_start'] == '2025-03-01'
    assert fetch.call_args.kwargs['params']['realtime_end'] == '2025-03-01'


def test_forward_asset_label_uses_fixed_complete_horizon_not_last_available_price():
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    graph = object.__new__(TradingAgentsGraph)
    frame = prices(15).set_index('timestamp')[['open']].rename(columns={'open': 'Open'})
    with patch('tradingagents.graph.trading_graph.yf.download', return_value=frame):
        value = graph._fetch_return('AAPL', datetime(2026,1,5).date(), 3, as_of=datetime(2026,2,1).date())
        partial = graph._fetch_return('AAPL', datetime(2026,1,5).date(), 3, as_of=datetime(2026,1,9).date())
    assert value == pytest.approx(104/101-1)
    assert partial is None


def test_empty_protective_submit_response_is_unknown_without_bare_retry(tmp_path):
    service, broker = live_fixture(tmp_path)
    broker.submit_order = Mock(return_value=None)
    with patch('tradingagents.safety.get_safety_guard', return_value=SimpleNamespace(enabled=False)), \
         patch('tradingagents.screening.gate.check_entry_allowed', return_value=None):
        result = service.execute(trade_intent=intent(datetime.now(timezone.utc)), dollar_amount=1000)
    assert not result['success']
    assert result['orders'][0]['status'] == 'UNKNOWN'
    assert broker.submit_order.call_count == 1
    assert broker.submit_order.call_args.args[0].stop_loss is not None
