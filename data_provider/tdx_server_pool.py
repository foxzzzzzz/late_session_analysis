"""通达信服务器池 — 健康检查 + 自动故障转移

背景: 大量 TDX 服务器能连、能查证券列表, 但 K 线返回空(实测143个中仅8个可用)。
且服务器状态"跳来跳去", 今天可用不代表明天可用。

策略:
1. 优先复用上次验证通过的服务器 (持久化到磁盘)
2. 当前服务器返回空 → 标记失败 → 自动换下一台
3. 池内全部失效 → 全量扫描 140+ 服务器 → 更新池

用法:
    pool = TdxServerPool()
    client = pool.get_client()          # 拿到可用 client
    pool.mark_failed(addr, port)        # 数据为空时标记, 下次自动换台
"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 探测超时 (秒)
PROBE_TIMEOUT = 5
# 全量扫描并发数
SCAN_WORKERS = 20
# 用于验证的探针股票 (平安银行, 深市)
PROBE_CODE = "000001"
PROBE_MARKET = 0


def _collect_servers() -> list[tuple[str, int]]:
    """汇总所有已知 TDX 服务器地址"""
    servers: list[tuple[str, int]] = []

    # tdxpy 内置 (约104个)
    try:
        from tdxpy.constants import hq_hosts
        servers.extend([(addr, port) for _, addr, port in hq_hosts])
    except ImportError:
        pass

    # mootdx 内置
    try:
        from mootdx.consts import HQ_HOSTS
        servers.extend([(addr, port) for _, addr, port in HQ_HOSTS])
    except ImportError:
        pass

    # 社区公认服务器 (补充分布在不同网络)
    servers.extend([
        ("119.147.212.81", 7709), ("119.147.212.82", 7709),
        ("59.36.5.11", 7709), ("202.100.166.27", 7709),
        ("218.108.98.244", 7709), ("60.191.117.167", 7709),
        ("114.80.149.19", 7709), ("114.80.149.22", 7709),
        ("218.75.126.9", 7709), ("115.238.90.165", 7709),
        ("115.238.56.198", 7709), ("124.160.88.183", 7709),
        ("180.153.39.51", 7709), ("218.6.170.47", 7709),
        ("123.125.108.14", 7709), ("180.153.18.170", 7709),
        ("180.153.18.171", 7709), ("202.108.253.130", 7709),
        ("202.108.253.131", 7709), ("60.28.29.69", 7709),
        ("218.60.29.136", 7709), ("117.34.114.14", 7709),
        ("117.34.114.15", 7709), ("117.34.114.16", 7709),
        ("117.34.114.17", 7709), ("117.34.114.18", 7709),
        ("117.34.114.20", 7709), ("117.34.114.27", 7709),
    ])

    # 去重保序
    return list(dict.fromkeys(servers))


def _probe_server(addr: str, port: int) -> bool:
    """验证服务器是否真能返回 K 线数据 (仅 TCP 通不算数)"""
    try:
        from pytdx.hq import TdxHq_API
    except ImportError:
        # pytdx 不可用时退到 mootdx
        return _probe_via_mootdx(addr, port)

    api = TdxHq_API()
    try:
        with api.connect(addr, port, time_out=PROBE_TIMEOUT):
            bars = api.get_security_bars(9, PROBE_MARKET, PROBE_CODE, 0, 5)
            return bool(bars) and len(bars) > 0
    except Exception:
        return False


def _probe_via_mootdx(addr: str, port: int) -> bool:
    """用 mootdx 验证 (pytdx 缺失时的备选)"""
    try:
        from mootdx.quotes import Quotes
        q = Quotes.factory(market="std", server=(addr, port))
        d = q.bars(symbol=PROBE_CODE, category=4, offset=5)
        return d is not None and len(d) > 0
    except Exception:
        return False


class TdxServerPool:
    """TDX 服务器池, 带健康检查与自动故障转移"""

    _CACHE_VERSION = 1

    def __init__(self, cache_file: str | Path | None = None):
        if cache_file is None:
            cache_file = Path.home() / ".mootdx" / "known_good.json"
        self._cache_file = Path(cache_file)
        self._lock = threading.Lock()
        # 已验证可用 (有序, 优先复用)
        self._good: list[tuple[str, int]] = []
        # 本次运行中确认失效
        self._failed: set[tuple[str, int]] = set()
        self._loaded = False

    # ---------------- 缓存读写 ----------------

    def _load_cache(self) -> None:
        """加载上次验证通过的服务器列表"""
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if data.get("version") != self._CACHE_VERSION:
                return
            for item in data.get("servers", []):
                addr, port = item[0], item[1]
                self._good.append((addr, int(port)))
            if self._good:
                logger.info(f"TDX 服务器池: 从缓存加载 {len(self._good)} 台")
        except (FileNotFoundError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            pass

    def _save_cache(self) -> None:
        """持久化可用服务器"""
        try:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": self._CACHE_VERSION,
                "servers": [[a, p] for a, p in self._good],
            }
            self._cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"TDX 服务器池缓存写入失败: {e}")

    # ---------------- 扫描 ----------------

    def scan(self, limit_workers: int = SCAN_WORKERS) -> list[tuple[str, int]]:
        """全量扫描所有已知服务器, 返回可用的 (按发现顺序)"""
        candidates = [s for s in _collect_servers() if s not in self._failed]
        logger.info(f"TDX 服务器池: 扫描 {len(candidates)} 台服务器...")

        found: list[tuple[str, int]] = []
        with ThreadPoolExecutor(max_workers=limit_workers) as ex:
            futures = {ex.submit(_probe_server, a, p): (a, p) for a, p in candidates}
            for f in as_completed(futures):
                addr, port = futures[f]
                try:
                    if f.result():
                        found.append((addr, port))
                except Exception:
                    pass

        logger.info(f"TDX 服务器池: 扫描完成, 可用 {len(found)} 台")
        if found:
            logger.info(f"TDX 服务器池: 可用服务器 {[f'{a}:{p}' for a, p in found[:5]]}...")
        return found

    def refresh(self) -> list[tuple[str, int]]:
        """重新扫描并更新池"""
        with self._lock:
            found = self.scan()
            if found:
                self._good = found
                self._failed.clear()
                self._save_cache()
            return list(self._good)

    # ---------------- 对外接口 ----------------

    def get_servers(self) -> list[tuple[str, int]]:
        """获取可用服务器列表 (必要时扫描)"""
        with self._lock:
            if not self._loaded:
                self._load_cache()
                self._loaded = True
            if self._good:
                return list(self._good)

        # 缓存为空: 扫描
        return self.refresh()

    def mark_failed(self, addr: str, port: int) -> None:
        """标记服务器失效, 后续不再使用"""
        with self._lock:
            key = (addr, int(port))
            self._failed.add(key)
            before = len(self._good)
            self._good = [s for s in self._good if s != key]
            removed = before - len(self._good)
        if removed:
            logger.warning(f"TDX 服务器池: {addr}:{port} 标记失效, 剩余 {len(self._good)} 台")
            self._save_cache()

    def has_usable(self) -> bool:
        with self._lock:
            return bool(self._good)

    def snapshot(self) -> dict[str, Any]:
        """诊断用快照"""
        with self._lock:
            return {
                "good": [f"{a}:{p}" for a, p in self._good],
                "failed": [f"{a}:{p}" for a, p in self._failed],
            }


# 模块级单例 (避免多处重复扫描)
_default_pool: Optional[TdxServerPool] = None
_default_pool_lock = threading.Lock()


def get_default_pool() -> TdxServerPool:
    """获取全局默认服务器池"""
    global _default_pool
    if _default_pool is None:
        with _default_pool_lock:
            if _default_pool is None:
                _default_pool = TdxServerPool()
    return _default_pool
