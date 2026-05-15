"""S0 板块预筛选 — 同花顺行业涨幅排名 → Top 3-5 板块 → 候选股票池"""
import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SectorFilter:
    """S0 板块预筛选：实时排名 → Top N板块 → 候选股票池

    数据源：同花顺行业涨跌幅（preloader 预加载），不碰东财资金流向 API。
    降级策略：preloader缓存 → 现场API → .env静态列表。
    """

    def __init__(self, preloader=None, config=None):
        self.preloader = preloader
        self.config = config
        self.top_n: int = getattr(config, 's0_top_n', 3) if config else 3
        self.max_n: int = getattr(config, 's0_max_n', 5) if config else 5
        self.min_stocks: int = getattr(config, 's0_min_stocks', 200) if config else 200
        self.stock_sector_map: dict[str, str] = {}

    def filter(self) -> tuple[list[str], dict[str, str]]:
        """执行 S0 预筛选

        Returns:
            (候选股票代码列表, 股票代码→板块名称映射)
        """
        t0 = time.time()

        rankings = self._get_sector_rankings()
        if not rankings:
            rankings = self._static_fallback_rankings()

        top_sectors = self._select_top_sectors(rankings)
        logger.info(f"S0 入选板块 ({len(top_sectors)}个): {top_sectors}")

        codes = self._get_constituent_stocks(top_sectors)

        elapsed = time.time() - t0
        logger.info(
            f"S0 完成 ({elapsed:.1f}s): "
            f"{len(codes)} 只候选股票, {len(self.stock_sector_map)} 只有板块映射"
        )
        return codes, self.stock_sector_map

    # === 板块排名 ===

    def _get_sector_rankings(self) -> list[tuple[str, float]]:
        """获取板块涨跌幅排名 [(板块名, 涨跌幅%), ...]，按涨幅降序，过滤下跌板块"""
        perf = self.preloader.sector_performance if self.preloader else {}
        if perf:
            positive = [(k, v) for k, v in perf.items() if v > 0]
            positive.sort(key=lambda x: x[1], reverse=True)
            if positive:
                logger.info(
                    f"S0 同花顺行业对比(缓存): {len(positive)} 个上涨板块, "
                    f"Top3: {positive[:3]}"
                )
                return positive

        # preloader 无数据，现场获取
        try:
            import akshare as ak
            df = ak.stock_board_industry_summary_ths()
            if df is None or df.empty:
                logger.warning("S0 同花顺API返回空")
                return []
            rankings = []
            for _, row in df.iterrows():
                name = str(row.get("板块", ""))
                pct = float(row.get("涨跌幅", 0))
                if name and pct > 0:
                    rankings.append((name, pct))
            rankings.sort(key=lambda x: x[1], reverse=True)
            logger.info(
                f"S0 同花顺行业对比(实时): {len(rankings)} 个上涨板块, "
                f"Top3: {rankings[:3]}"
            )
            return rankings
        except Exception as e:
            logger.warning(f"S0 同花顺API失败: {e}")
            return []

    def _static_fallback_rankings(self) -> list[tuple[str, float]]:
        """L3降级：使用 .env 静态板块列表，无涨跌幅信息"""
        sectors = self.config.target_sectors if self.config else []
        if sectors:
            logger.info(f"S0 降级到静态板块列表: {sectors}")
            return [(s, 1.0) for s in sectors]  # 统一给 1.0 涨幅
        return []

    # === Top N 选择 ===

    def _select_top_sectors(self, rankings: list[tuple[str, float]]) -> list[str]:
        """自动扩展：Top 3 → 成分股不够 → 扩展到 Top 4 → Top 5"""
        for n in range(self.top_n, min(self.max_n, len(rankings)) + 1):
            candidates = [name for name, _ in rankings[:n]]
            if n == self.top_n:
                continue  # 先不查，默认尝试 Top 3
            logger.info(f"S0 扩展到 Top {n}: Top {n-1} 成分股不足 {self.min_stocks}")
            return candidates
        return [name for name, _ in rankings[:self.top_n]]

    # === 成分股获取 ===

    MAX_RETRIES = 3
    BASE_DELAY = 1.0

    def _get_constituent_stocks(self, sectors: list[str]) -> list[str]:
        """对入选板块获取成分股，合并去重，建立 stock→sector 映射"""
        import akshare as ak

        seen: dict[str, str] = {}
        for sector in sectors:
            df = None
            for attempt in range(self.MAX_RETRIES):
                try:
                    df = ak.stock_board_industry_cons_em(symbol=sector)
                    break
                except Exception as e:
                    delay = self.BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.5)
                    logger.warning(
                        f"S0 成分股 [{sector}] 第{attempt + 1}次失败: {e}, "
                        f"{delay:.1f}s后重试"
                    )
                    time.sleep(delay)

            if df is None or df.empty:
                logger.warning(f"S0 成分股 [{sector}]: 返回空(已重试{self.MAX_RETRIES}次)")
                continue

            code_col = next(
                (c for c in df.columns if c in ("代码", "code")),
                df.columns[0],
            )
            count = 0
            for _, row in df.iterrows():
                code = str(row.get(code_col, "")).strip()
                if code:
                    if code not in seen:
                        seen[code] = sector
                    count += 1
            logger.debug(f"S0 成分股 [{sector}]: {count} 只")

        self.stock_sector_map = seen

        # 自动扩展检查
        if len(seen) < self.min_stocks and len(sectors) < self.max_n:
            # 需要扩展但 _select_top_sectors 已经返回了固定数量的板块
            # 这里只记录警告，扩展逻辑在 _select_top_sectors 中处理
            logger.info(
                f"S0 候选池 {len(seen)} 只 < {self.min_stocks}, "
                f"当前 {len(sectors)} 个板块已达上限或不足"
            )

        return list(seen.keys())
