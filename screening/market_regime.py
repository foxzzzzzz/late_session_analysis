"""市场状态判定 — 双因子投票 + 防抖 + 持久化

每日14:25管线启动时判定一次:
  - 因子1: 上证20日收益率, >= +2% → bull, <= -2% → bear
  - 因子2: 上证 vs MA20, above → bull, below → bear
  - 双因子投票 → bull/bear/neutral

防抖: 判定结果与昨日不同时, 维持昨日(连续2天同方向才切换), neutral除外(立即生效)
持久化: ~/.tradingagents/cache/market_regime.json
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _cache_path() -> Path:
    p = Path.home() / ".tradingagents" / "cache" / "market_regime.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_previous_regime() -> dict:
    """加载上一交易日判定的市场状态"""
    path = _cache_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, IOError):
            pass
    return {"regime": "neutral", "last_change": "", "consecutive_days": 0}


def _save_regime(state: dict):
    """持久化当前市场状态"""
    try:
        _cache_path().write_text(json.dumps(state, ensure_ascii=False))
    except IOError as e:
        logger.warning(f"市场状态持久化失败: {e}")


def _fetch_sh_index() -> Optional[pd.DataFrame]:
    """拉取上证指数日线 (mootdx, symbol=999999)"""
    try:
        from mootdx.quotes import Quotes
        client = Quotes.factory(market="std")
        df = client.index(symbol="999999", category=4, offset=50)
        if df is not None and not df.empty:
            return df
    except ImportError:
        logger.debug("mootdx 未安装, 无法获取上证指数")
    except Exception as e:
        logger.warning(f"上证指数获取失败: {e}")
    return None


def determine_regime(
    index_df: Optional[pd.DataFrame] = None,
    sector_performance: Optional[dict[str, float]] = None,
) -> str:
    """判定当前市场状态: bull / bear / neutral

    三因子投票 (范围 -3 ~ +3):
      - 因子1: 上证20日收益率, >= +2% → +1, <= -2% → -1
      - 因子2: 上证 vs MA20, above → +1, below → -1
      - 因子3: 行业涨跌比 (上涨行业数 / 下跌行业数), >= 1.5 → +1, <= 0.67 → -1

    Args:
        index_df: 上证指数日线DataFrame (mootdx格式, 含close列, 按时间升序)
        sector_performance: {行业名: 涨跌幅%} 同花顺90行业数据, 用于市场宽度因子
    Returns:
        "bull" | "bear" | "neutral"
    """
    if index_df is None:
        index_df = _fetch_sh_index()

    if index_df is None or index_df.empty:
        logger.warning("上证指数数据不可用, 使用中性阈值")
        return "neutral"

    close = pd.to_numeric(index_df["close"], errors="coerce").dropna()
    if len(close) < 25:
        logger.warning(f"上证日线不足25根 ({len(close)}), 使用中性阈值")
        return "neutral"

    # 因子1: 20日收益率
    ret_20d = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100
    ret_5d = (close.iloc[-1] - close.iloc[-6]) / close.iloc[-6] * 100

    # 因子2: vs MA20 (滞后指标, 短期动量改善时压制bear投票)
    ma20 = close.iloc[-20:].mean()
    above_ma20 = close.iloc[-1] > ma20

    # 因子3: 行业涨跌比 (市场宽度)
    breadth_up = breadth_down = 0
    if sector_performance:
        for pct in sector_performance.values():
            if pct > 0:
                breadth_up += 1
            elif pct < 0:
                breadth_down += 1

    bull_score = 0
    details = []

    if ret_20d >= 2:
        bull_score += 1
        details.append(f"20日{ret_20d:+.1f}%→bull")
    elif ret_20d <= -2:
        bull_score -= 1
        details.append(f"20日{ret_20d:+.1f}%→bear")
    else:
        details.append(f"20日{ret_20d:+.1f}%→neutral")

    if above_ma20:
        bull_score += 1
        details.append(f"MA20(above)→bull")
    elif ret_5d >= 1.0:
        # MA20 below 但短期动量已修复 — MA20只是滞后，不投bear
        details.append(f"MA20(below但5日{ret_5d:+.1f}%修复)→neutral")
    else:
        bull_score -= 1
        details.append(f"MA20(below)+5日{ret_5d:+.1f}%→bear")

    if sector_performance:
        total = breadth_up + breadth_down
        if total > 0:
            up_ratio = breadth_up / total
            if up_ratio >= 0.60:       # ≥60%行业上涨 → 市场宽度偏牛
                bull_score += 1
                details.append(f"行业宽度{up_ratio:.0%}({breadth_up}/{breadth_down})→bull")
            elif up_ratio <= 0.40:     # ≤40%行业上涨 → 市场宽度偏熊
                bull_score -= 1
                details.append(f"行业宽度{up_ratio:.0%}({breadth_up}/{breadth_down})→bear")
            else:
                details.append(f"行业宽度{up_ratio:.0%}({breadth_up}/{breadth_down})→neutral")
        else:
            details.append("行业涨跌(无数据)")
    else:
        details.append("行业涨跌(无数据)")

    if bull_score > 0:
        today_regime = "bull"
    elif bull_score < 0:
        today_regime = "bear"
    else:
        today_regime = "neutral"

    # 防抖
    final_regime = _debounce(today_regime)

    logger.info(
        f"市场状态: {final_regime} (score={bull_score:+d}) "
        f"[{', '.join(details)}]"
        f"{', 防抖维持' if final_regime != today_regime else ''}"
    )

    return final_regime


def _debounce(today_regime: str) -> str:
    """防抖: 单日方向变化不切换, 连续2天同方向才切。neutral立即生效。"""
    prev = _load_previous_regime()
    prev_regime = prev.get("regime", "neutral")
    prev_days = prev.get("consecutive_days", 0)

    if today_regime == prev_regime:
        # 同方向: 累计天数+1
        new_state = {
            "regime": today_regime,
            "last_change": prev.get("last_change", datetime.now().strftime("%Y-%m-%d")),
            "consecutive_days": prev_days + 1,
        }
        _save_regime(new_state)
        return today_regime

    # 方向变化
    if prev_regime == "neutral" or today_regime == "neutral":
        # neutral 立即生效
        new_state = {
            "regime": today_regime,
            "last_change": datetime.now().strftime("%Y-%m-%d"),
            "consecutive_days": 1,
        }
        _save_regime(new_state)
        return today_regime

    # bull ↔ bear 需要连续2天确认, 暂维持昨日
    pending_regime = prev.get("pending_regime")
    pending_days = prev.get("pending_days", 0)
    if pending_regime == today_regime:
        pending_days += 1
    else:
        pending_regime = today_regime
        pending_days = 1

    if pending_days >= 2:
        new_state = {
            "regime": today_regime,
            "last_change": datetime.now().strftime("%Y-%m-%d"),
            "consecutive_days": 1,
        }
        _save_regime(new_state)
        return today_regime

    logger.info(
        f"市场状态防抖: 今日={today_regime}, 昨日={prev_regime}({prev_days}天), "
        f"待确认={pending_regime}({pending_days}/2), 维持={prev_regime}"
    )
    new_state = {
        "regime": prev_regime,
        "last_change": prev.get("last_change", ""),
        "consecutive_days": prev_days,
        "pending_regime": pending_regime,
        "pending_days": pending_days,
    }
    _save_regime(new_state)
    return prev_regime


def force_regime(regime: str):
    """手动设置市场状态 (--regime bull|bear|neutral)"""
    valid = {"bull", "bear", "neutral"}
    if regime not in valid:
        raise ValueError(f"无效的市场状态: {regime}, 可选: {valid}")
    state = {
        "regime": regime,
        "last_change": datetime.now().strftime("%Y-%m-%d"),
        "consecutive_days": 1,
    }
    _save_regime(state)
    logger.info(f"市场状态手动设置: {regime}")
