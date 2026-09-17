"""Independent offline acceptance probes; no product changes or live calls."""
import sys
sys.path.insert(0, '/Users/zongen/Downloads/codex/tradingAlpaca/tests')
from types import SimpleNamespace as NS
import pandas as pd
import pytest
import tradingagents.agents
from langchain_core.messages import AIMessage, ToolMessage


def test_D01_infinite_volume_is_rejected():
    from tradingagents.dataflows.technical_brief import _clean_frame
    frame = pd.DataFrame([dict(timestamp='2026-09-15T14:00:00Z', open=100,
                               high=101, low=99, close=100, volume=float('inf'))])
    cleaned = _clean_frame(frame, pd.Timestamp('2026-09-15T15:00:00Z'))
    assert cleaned is None or cleaned.empty, f'Infinite volume survives: {cleaned.to_dict("records")}'


@pytest.mark.parametrize('timeframe,expected', [('4h', None), ('2Hour', None), ('1h', '1h'), ('4Hour', None)])
def test_D02_fallback_respects_requested_interval(monkeypatch, timeframe, expected):
    from tradingagents.dataflows import alpaca_utils as mod
    import yfinance
    seen = []
    monkeypatch.setattr(mod, 'get_config', lambda: {'data_fallback_enabled': True})
    def download(*args, **kwargs):
        seen.append(kwargs.get('interval'))
        return pd.DataFrame()
    monkeypatch.setattr(yfinance, 'download', download)
    mod._yfinance_fallback_data('AAPL', pd.Timestamp('2026-09-01'), None, timeframe)
    assert seen == ([] if expected is None else [expected]), f'{timeframe} reached Yahoo as {seen}'


def test_L02_actual_market_loop_preserves_native_tool_history(monkeypatch):
    from tradingagents.agents.analysts import market_analyst as mod
    from test_audit_analyst_tool_calls import _Chain, _FakeLLM, _Toolkit
    from langchain_anthropic.chat_models import _format_messages
    responses = [AIMessage(content='', tool_calls=[dict(name='get_stockstats_indicators_report',
                 args={'ticker':'AAPL','curr_date':'2026-09-17'}, id='call-native')]),
                 AIMessage(content='Final report based on evidence')]
    histories = []
    class Chain(_Chain):
        def invoke(self, msgs):
            histories.append(list(msgs))
            return super().invoke(msgs)
    monkeypatch.setattr(mod, 'ChatPromptTemplate', NS(from_messages=lambda *a, **k: Chain(responses)))
    monkeypatch.setattr(mod, 'capture_agent_prompt', lambda *a, **k: None)
    monkeypatch.setattr('tradingagents.regime.regime_report_block', lambda *a, **k: '')
    node = mod.create_market_analyst(_FakeLLM(), _Toolkit())
    node({'messages': [], 'company_of_interest':'AAPL', 'trade_date':'2026-09-17'})
    ai = next(m for m in histories[-1] if isinstance(m, AIMessage))
    tool = next(m for m in histories[-1] if isinstance(m, ToolMessage))
    _, wire = _format_messages([ai, tool])
    blocks = [b for m in wire for b in m['content'] if isinstance(b, dict)]
    assert any(b.get('type') == 'tool_use' and b.get('id') == 'call-native' for b in blocks), f'Native request lacks tool_use: {wire}; AI.tool_calls={ai.tool_calls}; invalid={ai.invalid_tool_calls}'


def test_U01_parallel_coordinator_cannot_write_stale_same_symbol_report(monkeypatch):
    from webui.utils import state as smod
    from tradingagents.graph.setup import GraphSetup
    app = smod.AppState()
    app.init_symbol_state('AAPL')
    app.analyzing_symbol = app.current_symbol = 'AAPL'
    monkeypatch.setattr(smod, 'app_state', app)
    owner = GraphSetup.__new__(GraphSetup)
    owner.config = {'analyst_call_delay':0, 'analyst_start_delay':0, 'tool_result_delay':0}
    def old_analyst(state):
        # Controlled Stop -> Start while the old provider call is in flight.
        app.request_stop()
        app.stop_requested = False
        app.get_state('AAPL')['current_reports']['market_report'] = 'NEW RUN REPORT'
        return {'messages': [], 'market_report':'OLD RUN REPORT', 'analysis_status':{'market':'completed'}}
    node = owner._create_parallel_analysts_coordinator(['market'], {'market':old_analyst},
                {'market':None}, {'market':lambda s:s})
    node({'company_of_interest':'AAPL', 'messages':[]})
    assert app.get_state('AAPL')['current_reports']['market_report'] == 'NEW RUN REPORT'


def test_U01_parallel_status_cannot_follow_another_symbol(monkeypatch):
    from webui.utils import state as smod
    from tradingagents.graph.setup import GraphSetup
    app = smod.AppState()
    app.init_symbol_state('AAPL'); app.init_symbol_state('MSFT')
    app.analyzing_symbol = app.current_symbol = 'AAPL'
    monkeypatch.setattr(smod, 'app_state', app)
    owner = GraphSetup.__new__(GraphSetup)
    owner.config = {'analyst_call_delay':0, 'analyst_start_delay':0, 'tool_result_delay':0}
    def old_analyst(state):
        app.request_stop(); app.stop_requested = False
        app.analyzing_symbol = 'MSFT'
        return {'messages': [], 'market_report':'AAPL report', 'analysis_status':{'market':'completed'}}
    node = owner._create_parallel_analysts_coordinator(['market'], {'market':old_analyst},
                {'market':None}, {'market':lambda s:s})
    node({'company_of_interest':'AAPL', 'messages':[]})
    assert app.get_state('MSFT')['agent_statuses']['Market Analyst'] == 'pending'


def test_D11_same_day_past_instant_never_fetches_present_quote(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from tradingagents.dataflows import interface as mod
    from tradingagents.dataflows.interface_utils import analysis_date_mode
    from tradingagents.dataflows.alpaca_utils import AlpacaUtils
    from test_backtest_engine import make_prices
    fixed_now = datetime(2026, 9, 16, 14, 0, tzinfo=ZoneInfo('America/New_York'))
    monkeypatch.setattr(mod, 'analysis_date_mode', lambda value: analysis_date_mode(value, now=fixed_now))
    monkeypatch.setattr(AlpacaUtils, 'get_stock_data', lambda **kwargs: make_prices([100,101,102]))
    seen = []
    monkeypatch.setattr(AlpacaUtils, 'get_latest_quote', lambda symbol: seen.append(symbol))
    mod.get_alpaca_data('AAPL', '2026-09-15', '2026-09-16T10:00:00-04:00', '1Hour')
    assert seen == [], f'Present quote requested after past exact cutoff: {seen}'


def test_D14_default_report_fetches_year_ago_month(monkeypatch):
    from tradingagents.dataflows import macro_utils as mod
    seen = []
    observations = [{'date':'2026-08-01','value':'310'}, {'date':'2025-08-01','value':'300'}]
    def fetch(series, start_date, end_date):
        seen.append((series, start_date, end_date))
        return {'observations':[row for row in observations if start_date <= row['date'] <= end_date]}
    monkeypatch.setattr(mod, 'get_fred_data', fetch)
    report = mod.get_economic_indicators_report('2026-09-16')
    assert 'Year-over-Year' in report, f'Available year-ago data excluded by requested windows: {seen}'
