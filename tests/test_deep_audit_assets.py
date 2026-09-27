"""Exercise the installed SDK transport with post-2026-09-22 asset JSON."""
from types import SimpleNamespace as NS

import pytest
from alpaca.trading.client import TradingClient

from tradingagents.dataflows import alpaca_utils as au
from tradingagents.screening import universe


ASSET = dict(id="00000000-0000-0000-0000-000000000001", symbol="AAPL", name="Apple",
             **{"class": "us_equity"}, exchange="NASDAQ", status="active", tradable=True,
             marginable=True, shortable=True, borrow_status="easy_to_borrow", fractionable=True)


@pytest.mark.parametrize("read_only", [True, False])
def test_all_factory_asset_reads_survive_removed_sdk_field(monkeypatch, read_only):
    sdk = TradingClient("fake-key", "fake-secret", paper=True)
    paths = []

    def get(path, data=None, **kwargs):
        paths.append(path)
        return [dict(ASSET)] if path == "/assets" else dict(ASSET)

    monkeypatch.setattr(sdk, "get", get)
    monkeypatch.setattr(au, "TradingClient", lambda *a, **k: sdk)
    monkeypatch.setattr(au, "get_api_key", lambda *a: "fake-key")
    monkeypatch.setattr(au, "get_alpaca_use_paper", lambda: True)
    monkeypatch.setattr(universe, "fetch_nasdaq_market_caps", lambda: {"AAPL": 1000000})
    client = au.get_alpaca_trading_client(read_only=read_only)
    assert universe.fetch_us_equity_universe(client)[0]["symbol"] == "AAPL"
    monkeypatch.setattr(au, "get_alpaca_trading_client", lambda *a, **k: client)
    assert au.AlpacaUtils.get_company_name("AAPL") == "Apple"
    assert au.get_alpaca_execution_client(read_only=read_only).get_asset("AAPL")["borrow_status"] == "easy_to_borrow"
    assert paths == ["/assets", "/assets/AAPL", "/assets/AAPL"]
    if read_only:
        with pytest.raises(au.PaperTradingEnforcementError):
            client.submit_order({})


def test_unknown_pagination_shape_cannot_silently_return_partial_universe(monkeypatch):
    monkeypatch.setattr(universe, "fetch_nasdaq_market_caps", lambda: {})
    asset = NS(symbol="AAPL", status="active", asset_class="us_equity", tradable=True)
    broker = NS(get_all_assets=lambda request: [asset, NS(next_page_token="more")])
    with pytest.raises(universe.UniverseError):
        universe.fetch_us_equity_universe(broker)


def test_last_page_asset_is_not_discarded():
    asset = NS(symbol="AAPL", next_page_token=None)
    assert universe._consume_pages(NS(get_all_assets=lambda _: [asset]), None) == [asset]


def test_repeating_pagination_token_cannot_loop_forever():
    calls = []

    def get_all_assets(request, page_token=None):
        calls.append(page_token)
        assert len(calls) < 4
        return [NS(next_page_token="same")]

    with pytest.raises(universe.UniverseError, match="repeated"):
        universe._consume_pages(NS(get_all_assets=get_all_assets), None)
