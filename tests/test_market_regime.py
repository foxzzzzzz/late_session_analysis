"""市场状态防抖测试"""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from screening import market_regime


@pytest.fixture
def index_source(monkeypatch):
    from data_provider import tdx_server_pool

    pool = Mock()
    pool.get_servers.return_value = [("first", 7709), ("second", 7709)]
    factory = Mock()
    monkeypatch.setitem(sys.modules, "mootdx.quotes", SimpleNamespace(
        Quotes=SimpleNamespace(factory=factory),
    ))
    monkeypatch.setattr(tdx_server_pool, "get_default_pool", lambda: pool)
    monkeypatch.setenv("MARKET_INDEX_BARS", "50")
    monkeypatch.setenv("MARKET_INDEX_TIMEOUT", "5")
    monkeypatch.setenv("MARKET_INDEX_MAX_SERVERS", "3")
    return pool, factory


def test_fetch_index_uses_index_endpoint_and_pool(index_source, caplog):
    pool, factory = index_source
    df = pd.DataFrame({"close": range(3000, 3050)})
    client = Mock()
    client.index.return_value = df
    factory.return_value = client

    with caplog.at_level("INFO"):
        result = market_regime._fetch_sh_index()

    pd.testing.assert_frame_equal(result, df)
    factory.assert_called_once_with(market="std", server=("first", 7709),
                                    timeout=5.0, auto_retry=False, raise_exception=True)
    client.index.assert_called_once_with(symbol="000001", frequency=9, offset=50)
    client.bars.assert_not_called()  # 不能误取深市000001个股
    client.close.assert_called_once()
    assert "first:7709" in caplog.text
    pool.mark_failed.assert_not_called()


@pytest.mark.parametrize("bad", [
    None, pd.DataFrame(), pd.DataFrame({"open": [3000] * 50}),
    pd.DataFrame({"close": [3000] * 24}),
    pd.DataFrame({"close": ["bad"] * 50}),
    pd.DataFrame({"close": [0] * 50}),
    pd.DataFrame({"close": [float("inf")] * 50}),
    RuntimeError("connection failed"),
])
def test_fetch_index_switches_on_bad_data(index_source, bad, caplog):
    pool, factory = index_source
    first, second = Mock(), Mock()
    if isinstance(bad, Exception):
        first.index.side_effect = bad
    else:
        first.index.return_value = bad
    valid = pd.DataFrame({"close": range(3000, 3050)})
    second.index.return_value = valid
    factory.side_effect = [first, second]

    result = market_regime._fetch_sh_index()

    pd.testing.assert_frame_equal(result, valid)
    assert factory.call_args_list[1].kwargs["server"] == ("second", 7709)
    first.close.assert_called_once()
    second.close.assert_called_once()
    assert "first:7709" in caplog.text
    # 指数失败不代表个股接口也失败，不能清除共享池中的服务器。
    pool.mark_failed.assert_not_called()


def test_fetch_index_honors_request_config(index_source, monkeypatch):
    _, factory = index_source
    monkeypatch.setenv("MARKET_INDEX_BARS", "60")
    monkeypatch.setenv("MARKET_INDEX_TIMEOUT", "2")
    monkeypatch.setenv("MARKET_INDEX_MAX_SERVERS", "1")
    factory.return_value.index.return_value = None

    assert market_regime._fetch_sh_index() is None
    assert factory.call_count == 1
    assert factory.call_args.kwargs["timeout"] == 2.0
    factory.return_value.index.assert_called_once_with(symbol="000001", frequency=9, offset=60)


def test_all_index_servers_fail_returns_neutral(index_source, monkeypatch, caplog):
    _, factory = index_source
    factory.return_value.index.return_value = None
    debounce = Mock()
    monkeypatch.setattr(market_regime, "_debounce", debounce)

    assert market_regime.determine_regime() == "neutral"
    assert factory.call_count == 2
    assert "全部失败" in caplog.text
    assert "使用中性阈值" in caplog.text
    debounce.assert_not_called()


@pytest.mark.parametrize("closes,expected", [
    (list(range(3000, 3050)), "bull"),
    (list(range(3050, 3000, -1)), "bear"),
])
def test_valid_index_reaches_regime_vote(index_source, monkeypatch, closes, expected):
    _, factory = index_source
    factory.return_value.index.return_value = pd.DataFrame({"close": closes})
    monkeypatch.setattr(market_regime, "_debounce", lambda regime: regime)

    assert market_regime.determine_regime() == expected


def test_debounce_switches_after_two_consecutive_opposite_days(monkeypatch):
    """bull/bear 反向连续两天应完成切换，而不是永久维持旧状态"""
    states = [{"regime": "bull", "consecutive_days": 3, "last_change": "2026-05-01"}]
    saved = []

    def fake_load():
        return states[-1]

    def fake_save(state):
        saved.append(state)
        states.append(state)

    monkeypatch.setattr(market_regime, "_load_previous_regime", fake_load)
    monkeypatch.setattr(market_regime, "_save_regime", fake_save)

    first = market_regime._debounce("bear")
    second = market_regime._debounce("bear")

    assert first == "bull"
    assert second == "bear"
    assert saved[-1]["regime"] == "bear"
