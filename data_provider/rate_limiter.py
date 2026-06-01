"""东财统一请求门控 — 串行化 + 最小间隔 + 随机抖动 + Session复用

借鉴 TradingAgents-astock v0.2.11 方案。
所有指向 eastmoney.com 的 HTTP 请求统一收口，避免触发东财反爬封禁。

东财风控阈值 (社区实测):
  - 每秒 >5 次 → 触发
  - 并发 ≥10 → 触发
  - 1分钟 ≥200 → 触发
  - 5分钟 ≥300 → 触发

本模块保证 QPS ≤ 1 (EM_MIN_INTERVAL 默认 1.0s)，批量可设 1.5~2s。

非东财源 (mootdx/腾讯/新浪/同花顺/财联社/百度) 不经过此门控。
"""
import logging
import os
import random
import threading
import time
import urllib.request
from http.client import HTTPResponse
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 最小请求间隔 (秒)，环境变量可覆盖，批量场景推荐 1.5~2.0
EM_MIN_INTERVAL = float(os.getenv("EM_MIN_INTERVAL", "1.0"))

# 共享 Session (Keep-Alive，减少 TCP 握手)
_em_session: Optional[requests.Session] = None
_last_em_call: float = 0.0
_lock = threading.Lock()

# 默认 UA (各端点可覆盖)
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "Chrome/117.0.0.0 Safari/537.36"
)


def _get_em_session() -> requests.Session:
    """获取或创建共享 Session"""
    global _em_session
    if _em_session is None:
        _em_session = requests.Session()
        _em_session.headers.update({"User-Agent": _DEFAULT_UA})
    return _em_session


def em_get(url: str, timeout: float = 15, **kwargs) -> requests.Response:
    """东财统一 GET 请求 — 串行化门控

    所有 eastmoney.com 的 requests 调用统一走此入口。
    自动控制请求间隔 ≥ EM_MIN_INTERVAL + 随机抖动。

    Args:
        url: 请求 URL
        timeout: 超时秒数
        **kwargs: 传给 requests.Session.get() 的额外参数 (如 headers)

    Returns:
        requests.Response 对象

    Raises:
        requests.RequestException: 网络错误直接抛出，由调用方处理
    """
    global _last_em_call
    with _lock:
        elapsed = time.time() - _last_em_call
        if elapsed < EM_MIN_INTERVAL and _last_em_call > 0:
            wait = EM_MIN_INTERVAL - elapsed + random.uniform(0.1, 0.5)
            time.sleep(wait)
        try:
            resp = _get_em_session().get(
                url, timeout=timeout,
                **kwargs,
            )
            _last_em_call = time.time()
            return resp
        except requests.RequestException:
            _last_em_call = time.time()
            raise


def em_urlopen(
    url: str, timeout: float = 10, headers: dict = None,
) -> HTTPResponse:
    """东财统一 URL open — 用于 push2 (必须 urllib，requests UA 被封)

    与 em_get 共享同一锁和间隔控制。
    针对 push2.eastmoney.com 的特殊 User-Agent 处理。

    Args:
        url: 请求 URL
        timeout: 超时秒数
        headers: 自定义请求头 (User-Agent, Referer 等)

    Returns:
        urllib HTTPResponse 对象
    """
    global _last_em_call
    with _lock:
        elapsed = time.time() - _last_em_call
        if elapsed < EM_MIN_INTERVAL and _last_em_call > 0:
            wait = EM_MIN_INTERVAL - elapsed + random.uniform(0.1, 0.5)
            time.sleep(wait)
        try:
            req = urllib.request.Request(url)
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            if "User-Agent" not in (headers or {}):
                req.add_header("User-Agent", _DEFAULT_UA)
            resp = urllib.request.urlopen(req, timeout=timeout)
            _last_em_call = time.time()
            return resp
        except Exception:
            _last_em_call = time.time()
            raise


def em_request_interval() -> float:
    """返回当前距上次调用的间隔 (用于诊断日志)"""
    if _last_em_call == 0:
        return 0.0
    return time.time() - _last_em_call
