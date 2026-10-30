"""新浪HTTP备用K线；失败或数据不完整时返回None，不合成行情。"""
import json
import logging
import os
import urllib.request

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
_URL = ('https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/'
        'CN_MarketData.getKLineData?symbol={symbol}&scale={scale}&ma=no&datalen={bars}')


def _now():
    return pd.Timestamp.now(tz='Asia/Shanghai').tz_localize(None)


def _fetch(symbol, scale, bars):
    try:
        req = urllib.request.Request(_URL.format(symbol=symbol, scale=scale, bars=bars),
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=float(os.getenv('HTTP_KLINE_TIMEOUT', '5'))) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if not isinstance(data, list) or not data:
            raise ValueError('空数据或非列表响应')
        df = pd.DataFrame(data).rename(columns={'day': 'datetime'})
        df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].copy()
        df['datetime'] = pd.to_datetime(df['datetime'], errors='raise')
        if df['datetime'].isna().any() or df['datetime'].duplicated().any():
            raise ValueError('时间缺失或重复')
        for col in ('open', 'high', 'low', 'close', 'volume'):
            df[col] = pd.to_numeric(df[col], errors='raise')
            if not np.isfinite(df[col]).all() or (df[col] < 0).any():
                raise ValueError(f'{col}包含无效值')
        if (df[['open', 'high', 'low', 'close']] <= 0).any().any():
            raise ValueError('非正价格')
        if ((df['high'] < df[['open', 'close', 'low']].max(axis=1)) |
                (df['low'] > df[['open', 'close', 'high']].min(axis=1))).any():
            raise ValueError('OHLC价格关系无效')
        df = df.sort_values('datetime').reset_index(drop=True)
        # 新浪volume单位为股；该接口没有真实成交额，沿用既有日线估算口径。
        df['amount'] = df['close'] * df['volume']
        df['turnover'] = df['amount']
        df.attrs.update(source='sina_http', amount_estimated=True)
        return df
    except Exception as exc:
        logger.warning(f'HTTP K线失败 {symbol} scale={scale}: {exc}')
        return None


def fetch_index_daily(bars=50, now=None):
    now = _now() if now is None else pd.Timestamp(now)
    df = _fetch('sh000001', 240, bars)
    if df is None:
        return None
    latest = df['datetime'].iloc[-1]
    max_age = int(os.getenv('HTTP_INDEX_MAX_AGE_DAYS', '7'))
    if len(df) < 25 or latest > now or now.normalize() - latest.normalize() > pd.Timedelta(days=max_age):
        logger.warning('HTTP上证日线不足、过期或时间异常')
        return None
    logger.info(f'HTTP上证日线成功: {len(df)}根, 末条={latest}; 可能仅含已完成交易日日线')
    return df


def fetch_stock_5min(code, bars=48, now=None):
    from data_provider.kline_provider import KlineProvider
    now = _now() if now is None else pd.Timestamp(now)
    code = str(code).zfill(6)
    prefix = KlineProvider._sina_code_prefix(code)
    request_bars = max(bars, int(os.getenv('HTTP_5MIN_FETCH_BARS', '96')))
    df = _fetch(prefix + code, 5, request_bars)
    if df is None:
        return None
    # 去掉昨日及尚未结束的bar，避免混用多个交易日或未来数据。
    df = df[(df['datetime'].dt.normalize() == now.normalize()) &
            (df['datetime'] <= now.floor('5min'))].copy()
    if df.empty:
        logger.warning(f'HTTP 5分钟线缺少当日完整bar: {code}')
        return None
    day = now.normalize()
    times = pd.date_range(day + pd.Timedelta(hours=9, minutes=35), periods=24, freq='5min')
    times = times.append(pd.date_range(day + pd.Timedelta(hours=13, minutes=5), periods=24, freq='5min'))
    expected = times[times <= now.floor('5min')]
    latest = df['datetime'].iloc[-1]
    lag = float(os.getenv('HTTP_5MIN_MAX_LAG_MINUTES', '5'))
    # 以交易时段终点检查时效，午休和收盘后不按墙上时间误判过期。
    if (expected.empty or not df['datetime'].isin(times).all() or latest > expected[-1] or
            expected[-1] - latest > pd.Timedelta(minutes=lag) or
            not times[times <= latest].isin(df['datetime']).all()):
        logger.warning(f'HTTP 5分钟线不完整或延迟过大: {code}, 末条={latest}')
        return None
    df.attrs.update(source='sina_http', amount_estimated=True)
    return df.reset_index(drop=True)
