"""Regression acceptance for the six remaining audit repairs (offline only)."""
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


@pytest.mark.parametrize('analyst,tool_name,report_key', [
    ('market_analyst', 'get_stockstats_indicators_report', 'market_report'),
    ('fundamentals_analyst', 'get_sec_ir_source', 'fundamentals_report'),
    ('macro_analyst', 'get_macro_analysis', 'macro_report'),
    ('news_analyst', 'get_finnhub_news_recent', 'news_report'),
    ('social_media_analyst', 'get_reddit_stock_info', 'sentiment_report'),
])
@pytest.mark.parametrize('raw', [False, True])
def test_all_analysts_preserve_one_assistant_turn_with_multiple_calls(monkeypatch, analyst, tool_name, report_key, raw):
    import importlib
    from test_audit_analyst_tool_calls import _Chain, _FakeLLM, _ToolFn
    from langchain_anthropic.chat_models import _format_messages
    from langchain_google_genai.chat_models import _parse_chat_history
    mod = importlib.import_module('tradingagents.agents.analysts.' + analyst)
    class Toolkit:
        config = {'online_tools':False, 'max_tool_iterations_per_agent':1}
        def __getattr__(self, name):
            if name.startswith('has_'):
                return lambda: name in ('has_fred', 'has_finnhub')
            return _ToolFn(name)
    calls = [dict(name=tool_name, args={'ticker':'AAPL','curr_date':'2026-09-17'}, id='call-a'),
             dict(name=tool_name, args={'ticker':'MSFT','curr_date':'2026-09-17'}, id='call-b')]
    kwargs = {'provider_marker':'preserve-me'}
    if raw:
        kwargs['tool_calls'] = [dict(id=c['id'], type='function', function=dict(name=c['name'], arguments=__import__('json').dumps(c['args']))) for c in calls]
        request = AIMessage(content='Using two evidence sources', additional_kwargs=kwargs)
    else:
        request = AIMessage(content='Using two evidence sources', tool_calls=calls, additional_kwargs=kwargs)
    histories=[]
    class Chain(_Chain):
        def invoke(self, msgs):
            histories.append(list(msgs))
            return super().invoke(msgs)
    responses = [request, AIMessage(content='Final evidence report')]
    monkeypatch.setattr(mod, 'ChatPromptTemplate', NS(from_messages=lambda *a, **k: Chain(responses)))
    monkeypatch.setattr(mod, 'capture_agent_prompt', lambda *a, **k: None)
    monkeypatch.setattr('tradingagents.regime.regime_report_block', lambda *a, **k: '')
    factory = getattr(mod, 'create_' + analyst)
    out = factory(_FakeLLM(), Toolkit())({'messages':[], 'company_of_interest':'AAPL', 'trade_date':'2026-09-17'})
    assert 'Final evidence report' in out[report_key]
    history = histories[-1]
    assert [type(m) for m in history] == [AIMessage, ToolMessage, ToolMessage]
    assert [c['id'] for c in history[0].tool_calls] == ['call-a','call-b']
    assert history[0].additional_kwargs['provider_marker'] == 'preserve-me'
    _, wire = _format_messages(history)
    uses = [b['id'] for m in wire for b in m['content'] if isinstance(b,dict) and b.get('type')=='tool_use']
    assert uses == ['call-a','call-b']
    _, google = _parse_chat_history(history)
    assert len(google[0].parts) == 2
    assert all(part.function_call.name == tool_name for part in google[0].parts)


@pytest.mark.parametrize('analyst,tool_name,report_key', [
    ('fundamentals_analyst', 'get_sec_ir_source', 'fundamentals_report'),
    ('macro_analyst', 'get_macro_analysis', 'macro_report'),
    ('news_analyst', 'get_finnhub_news_recent', 'news_report'),
    ('social_media_analyst', 'get_reddit_stock_info', 'sentiment_report'),
])
def test_native_budget_exhaustion_does_not_publish_tool_request_as_report(monkeypatch, analyst, tool_name, report_key):
    import importlib
    from test_audit_analyst_tool_calls import _Chain, _FakeLLM, _ToolFn
    mod = importlib.import_module('tradingagents.agents.analysts.'+analyst)
    class Toolkit:
        config = {'online_tools':False, 'max_tool_iterations_per_agent':1}
        def __getattr__(self,name):
            return (lambda: name in ('has_fred','has_finnhub')) if name.startswith('has_') else _ToolFn(name)
    request = AIMessage(content='Intermediate text is not a report', tool_calls=[dict(name=tool_name, args={}, id='call-limit')])
    monkeypatch.setattr(mod,'ChatPromptTemplate', NS(from_messages=lambda *a,**k:_Chain([request])))
    monkeypatch.setattr(mod,'capture_agent_prompt',lambda *a,**k:None)
    out=getattr(mod,'create_'+analyst)(_FakeLLM(),Toolkit())({'messages':[],'company_of_interest':'AAPL','trade_date':'2026-09-17'})
    assert not out[report_key]






@pytest.mark.parametrize('volume', [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_volume_never_reaches_indicators(volume):
    from tradingagents.dataflows.technical_brief import _clean_frame
    frame=pd.DataFrame([dict(timestamp='2026-09-15T14:00:00Z',open=100,high=101,low=99,close=100,volume=volume)])
    assert _clean_frame(frame,pd.Timestamp('2026-09-15T15:00:00Z')) is None


def test_valid_numeric_strings_become_numeric_indicator_inputs():
    from tradingagents.dataflows.technical_brief import _clean_frame
    frame=pd.DataFrame([dict(timestamp='2026-09-15T14:00:00Z',open='100',high='101',low='99',close='100',volume='0')])
    cleaned=_clean_frame(frame,pd.Timestamp('2026-09-15T15:00:00Z'))
    assert cleaned is not None
    assert all(pd.api.types.is_numeric_dtype(cleaned[column]) for column in ('open','high','low','close','volume'))


@pytest.mark.parametrize('timeframe,expected', [('1Hour','1h'),('1Day','1d'),('1d','1d'),('2h',None),('1Min',None),('nonsense',None),('0Hour',None),('2Day',None)])
def test_fallback_exact_granularity_whitelist(monkeypatch,timeframe,expected):
    test_D02_fallback_respects_requested_interval(monkeypatch,timeframe,expected)


@pytest.mark.parametrize('hours', [1,2,4])
def test_fallback_accepts_only_supported_sdk_timeframes(monkeypatch,hours):
    from alpaca.data.timeframe import TimeFrame,TimeFrameUnit
    test_D02_fallback_respects_requested_interval(monkeypatch,TimeFrame(hours,TimeFrameUnit.Hour),'1h' if hours==1 else None)


def test_macro_extends_only_yoy_series_and_reports_missing_month(monkeypatch):
    from tradingagents.dataflows import macro_utils as mod
    seen={}
    def fetch(series,start_date,end_date):
        seen[series]=start_date
        return {'observations':[{'date':'2026-08-01','value':'310'}]}
    monkeypatch.setattr(mod,'get_fred_data',fetch)
    report=mod.get_economic_indicators_report('2026-09-16',lookback_days=30)
    assert seen['FEDFUNDS']=='2026-08-17'
    assert seen['CPIAUCSL']<='2025-08-01' and seen['PPIACO']<='2025-08-01'
    assert 'Year-over-Year' not in report  # no invented comparison for absent data
