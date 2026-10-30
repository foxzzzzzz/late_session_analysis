import json
from unittest.mock import Mock

import pandas as pd
import pytest

from data_provider import http_kline as source


NOW = pd.Timestamp('2026-10-30 14:40')


def minute_rows():
    times = list(pd.date_range('2026-10-30 09:35', periods=24, freq='5min'))
    times += list(pd.date_range('2026-10-30 13:05', periods=24, freq='5min'))
    return [dict(day=str(t), open='10', close='10.1', high='10.2', low='9.9', volume='100')
            for t in times]


def respond(monkeypatch, rows):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps(rows).encode()
    request = Mock(return_value=response)
    monkeypatch.setattr(source.urllib.request, 'urlopen', request)
    return request


def test_minute_prefix_volume_complete_bars(monkeypatch):
    request = respond(monkeypatch, minute_rows())
    df = source.fetch_stock_5min('000001', now=NOW)
    assert 'symbol=sz000001' in request.call_args.args[0].full_url
    assert df.iloc[0]['volume'] == 100  # 新浪为股，不能再乘100。
    assert df.iloc[-1]['datetime'] == pd.Timestamp('2026-10-30 14:40')
    assert df['datetime'].dt.date.nunique() == 1
    assert df.attrs['amount_estimated'] is True


@pytest.mark.parametrize('bad', ['stale', 'duplicate', 'invalid', 'gap', 'late'])
def test_reject_bad_minute_data(monkeypatch, bad):
    rows = minute_rows()
    if bad == 'stale':
        rows = [dict(r, day=r['day'].replace('10-30', '10-29')) for r in rows]
    elif bad == 'duplicate':
        rows.append(rows[0])
    elif bad == 'invalid':
        rows[0]['close'] = 'nan'
    elif bad == 'gap':
        rows.pop(5)
    else:
        rows = rows[:24]
    respond(monkeypatch, rows)
    assert source.fetch_stock_5min('600519', now=NOW) is None


def test_index_uses_shanghai_and_completed_daily(monkeypatch):
    rows = [dict(day=str(t.date()), open='3000', high='3100', low='2900',
                 close='3050', volume='1000')
            for t in pd.bdate_range(end='2026-10-29', periods=50)]
    request = respond(monkeypatch, rows)
    df = source.fetch_index_daily(50, now=NOW)
    assert len(df) == 50
    assert 'symbol=sh000001' in request.call_args.args[0].full_url
    assert df.attrs['source'] == 'sina_http'


def test_http_error_returns_missing(monkeypatch):
    monkeypatch.setattr(source.urllib.request, 'urlopen', Mock(side_effect=TimeoutError('timeout')))
    assert source.fetch_index_daily(50, now=NOW) is None


@pytest.mark.parametrize('end', ['2026-01-01', '2026-11-01'])
def test_index_rejects_stale_or_future(monkeypatch, end):
    rows = [dict(day=str(t.date()), open='3000', high='3100', low='2900',
                 close='3050', volume='1000') for t in pd.date_range(end=end, periods=50)]
    respond(monkeypatch, rows)
    assert source.fetch_index_daily(50, now=NOW) is None


def test_partial_tdx_failure_only_fetches_missing(monkeypatch):
    from data_provider.kline_provider import KlineProvider
    provider = KlineProvider.__new__(KlineProvider)
    provider._client = Mock()
    tdx_df = pd.DataFrame({'close': [10], 'amount': [100]})
    provider._client.bars.side_effect = lambda symbol, **kwargs: tdx_df if symbol == '000001' else None
    http_df = pd.DataFrame({'close': [20]})
    http = Mock(return_value=http_df)
    monkeypatch.setattr(source, 'fetch_stock_5min', http)
    result = provider.load_5min_batch(['000001', '600519'])
    assert set(result) == {'000001', '600519'}
    http.assert_called_once_with('600519', 48)


def test_no_tdx_still_fetches_http(monkeypatch):
    from data_provider.kline_provider import KlineProvider
    provider = KlineProvider.__new__(KlineProvider)
    provider._client = None
    provider._client_error = 'unavailable'
    http = Mock(return_value=pd.DataFrame({'close': [10]}))
    monkeypatch.setattr(source, 'fetch_stock_5min', http)
    assert '000001' in provider.load_5min_batch(['000001'])
    http.assert_called_once()


def test_index_empty_tdx_pool_reaches_http(monkeypatch):
    from screening import market_regime
    from data_provider import tdx_server_pool
    pool = Mock()
    pool.get_servers.return_value = []
    monkeypatch.setattr(tdx_server_pool, 'get_default_pool', lambda: pool)
    expected = pd.DataFrame({'close': [3000] * 50})
    fallback = Mock(return_value=expected)
    monkeypatch.setattr(market_regime, '_fetch_http_index', fallback)
    assert market_regime._fetch_sh_index() is expected
    fallback.assert_called_once()
