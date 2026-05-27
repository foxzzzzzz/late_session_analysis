"""北向资金情绪 — 同花顺 hsgtApi 上一交易日数据 + 本地自缓存

注意: dayChart 端点返回最近完整交易日数据(非当日实时)，盘中调用也是昨日数据。
数据源: data.hexin.cn/market/hsgtApi/method/dayChart/
自缓存: ~/.tradingagents/cache/northbound_daily.csv (收盘后自动写入)
"""
import logging
import requests
import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

HSGT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def _cache_path() -> Path:
    p = Path.home() / ".tradingagents" / "cache" / "northbound_daily.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_history(n: int = 20) -> pd.DataFrame:
    """读取最近 N 天北向历史 (排除当日，避免和实时数据重复)"""
    path = _cache_path()
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        today_str = datetime.now().strftime("%Y-%m-%d")
        df = df[df["date"] != today_str]
        return df.tail(n)
    except Exception:
        return pd.DataFrame()


def _save_snapshot(date: str, hgt: float, sgt: float):
    """写入/更新当天北向收盘数据到 CSV

    仅保存合理值: 单日净买入在 ±200亿 范围内。
    """
    net = hgt + sgt
    if abs(net) > 200:
        logger.debug(f"北向缓存: 跳过异常值 {date} net={net:.1f}亿")
        return

    path = _cache_path()
    rows = {}
    if path.exists():
        try:
            for line in path.read_text().strip().split("\n")[1:]:
                parts = line.split(",")
                if len(parts) == 3:
                    rows[parts[0]] = line
        except Exception:
            pass
    rows[date] = f"{date},{hgt},{sgt}"
    with open(path, "w") as f:
        f.write("date,hgt,sgt\n")
        for d in sorted(rows.keys()):
            f.write(rows[d] + "\n")


def fetch_northbound_realtime() -> Optional[dict]:
    """拉取最近完整交易日北向资金分钟数据 (非当日实时，T+1滞后)

    Returns:
        dict with keys: today_net_yi (最近交易日累计净买入), hgt_yi, sgt_yi,
                        trend_score (近N日趋势), points (分钟数据点数),
                        recent_days (近N日每日净买入)
        失败返回 None
    """
    try:
        url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = requests.get(url, headers=HSGT_HEADERS, timeout=10)
        d = r.json()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])

        if not times or not hgt:
            return None

        # 取最后一个有效值作为当日累计
        today_hgt = 0.0
        today_sgt = 0.0
        for v in reversed(hgt):
            if v is not None:
                today_hgt = float(v)
                break
        for v in reversed(sgt):
            if v is not None:
                today_sgt = float(v)
                break

        today_net = today_hgt + today_sgt

        # 调试: 打印API返回的时间范围，确认是哪天的数据
        first_time = times[0] if times else "N/A"
        last_time = times[-1] if times else "N/A"
        logger.info(
            f"北向API返回: times={first_time}~{last_time}, "
            f"points={len(times)}, "
            f"hgt末值={today_hgt:.2f}, sgt末值={today_sgt:.2f}, "
            f"net={today_net:.2f}"
        )

        # 加载历史计算趋势
        hist = _load_history(20)
        recent_values = []
        if not hist.empty:
            hist["net"] = hist["hgt"].astype(float) + hist["sgt"].astype(float)
            recent_values = hist["net"].tail(20).tolist()

        # 趋势分数: 正值天数占比 + 累计净额趋势
        trend_score = _calc_trend_score(recent_values + [today_net])

        # 仅在当日收盘后缓存 (15:00后且当前时间在交易时段之后)
        # 避免把前一交易日数据错误写入当前日期
        now = datetime.now()
        should_cache = False
        if times:
            last_time = times[-1]
            if isinstance(last_time, str) and last_time.strip() == "15:00":
                # 最后数据点是15:00 → 当日已收盘，但仅在15:00后缓存
                # (排除盘前拿到前一交易日收盘数据的情况)
                if now.hour >= 15:
                    should_cache = True
        if not should_cache and (now.hour > 15 or (now.hour == 15 and now.minute >= 5)):
            should_cache = True

        if should_cache:
            today_str = now.strftime("%Y-%m-%d")
            _save_snapshot(today_str, today_hgt, today_sgt)

        return {
            "today_net_yi": round(today_net, 2),
            "hgt_yi": round(today_hgt, 2),
            "sgt_yi": round(today_sgt, 2),
            "trend_score": trend_score,
            "points": len(times),
            "recent_days": recent_values[-5:] if recent_values else [],
        }

    except Exception as e:
        logger.warning(f"北向资金获取失败: {e}")
        return None


def _calc_trend_score(recent_nets: list[float]) -> float:
    """计算北向资金趋势分数 0-100

    - 近N日正值天数占比 × 50
    - 近5日累计净额 vs 近20日 趋势强度 × 50
    """
    if not recent_nets:
        return 50.0

    if len(recent_nets) == 1:
        val = recent_nets[0]
        if val > 50:
            return 80.0
        elif val > 20:
            return 65.0
        elif val > -20:
            return 50.0
        elif val > -50:
            return 35.0
        else:
            return 20.0

    score = 0.0

    # 正值天数占比
    positive_days = sum(1 for v in recent_nets if v > 0)
    score += (positive_days / len(recent_nets)) * 50

    # 近5日 vs 近20日 趋势加速
    n = len(recent_nets)
    recent_5 = sum(recent_nets[-5:]) if n >= 5 else sum(recent_nets) / n * 5
    recent_all = sum(recent_nets)
    avg_20 = recent_all / n * 5  # 折算成5日均值

    if avg_20 > 0 and recent_5 > 0:
        ratio = recent_5 / max(avg_20, 0.01)
        if ratio >= 2:
            score += 40
        elif ratio >= 1.5:
            score += 30
        elif ratio >= 1.0:
            score += 20
        elif ratio >= 0.5:
            score += 10
    elif recent_5 > 0 and avg_20 <= 0:
        score += 40  # 近期转向流入
    elif avg_20 > 0 and recent_5 <= 0:
        score += 5   # 近期转向流出
    elif recent_5 < 0 and avg_20 < 0:
        score += 10  # 持续流出但可能触底

    return min(score, 100)


def get_northbound_sentiment() -> dict:
    """获取北向资金情绪摘要 (供报告/L4评分使用)

    Returns:
        {"available": bool, "sentiment": "inflow"/"neutral"/"outflow",
         "today_net_yi": float, "trend_score": float,
         "trend_label": "strong_inflow"/"moderate_inflow"/"neutral"/"outflow"}
    """
    data = fetch_northbound_realtime()
    if not data:
        return {"available": False, "sentiment": "unknown", "today_net_yi": 0, "trend_score": 50}

    net = data["today_net_yi"]
    trend = data["trend_score"]

    if net > 20:
        sentiment = "inflow"
    elif net < -20:
        sentiment = "outflow"
    else:
        sentiment = "neutral"

    if trend >= 70:
        trend_label = "strong_inflow"
    elif trend >= 55:
        trend_label = "moderate_inflow"
    elif trend >= 45:
        trend_label = "neutral"
    else:
        trend_label = "outflow"

    return {
        "available": True,
        "sentiment": sentiment,
        "today_net_yi": net,
        "trend_score": trend,
        "trend_label": trend_label,
    }
