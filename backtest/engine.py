"""回测引擎 — 逐交易日循环 S1→S2→S3→S4"""
import logging
import time
from typing import Optional

import pandas as pd

from screening.layer1_access import screen_l1_access, L1Config
from screening.layer2_anomaly import screen_l2_anomaly, L2Config
from screening.layer3_technical import screen_l3_technical, L3Config
from screening.layer4_scoring import score_l4, L4Config, set_northbound_sentiment, set_concept_analyzer
from screening.layer_kline import screen_kline, KlineConfig
from analysis.merger import merge_and_rank
from backtest.config import BacktestConfig
from backtest.data_loader import BacktestDataLoader
from backtest.data_adapter import HistoricalDataAdapter
from backtest.trade_log import Trade, DayResult, TradeLogRecorder
from backtest.performance import PerformanceCalculator
from backtest.report_generator import BacktestReportGenerator

logger = logging.getLogger(__name__)


class BacktestEngine:
    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.loader = BacktestDataLoader(self.config)
        self.trade_log = TradeLogRecorder()

        # 板块成分股
        self.sector_codes: dict[str, list[str]] = {}
        self.all_candidate_codes: list[str] = []
        self.sector_map: dict[str, str] = {}

        # 筛选配置 (复用实时管线的阈值)
        self.l1_config = L1Config(
            min_turnover=self.config.l1_min_turnover,
            min_turnover_rate=self.config.l1_min_turnover_rate,
            min_price=self.config.l1_min_price,
            max_price=self.config.l1_max_price,
        )
        self.l2_config = L2Config(
            volume_ratio_min=self.config.l2_volume_ratio_min,
            last_5min_vol_pct_min=self.config.l2_last_5min_vol_pct_min,
            late_rally_min=self.config.l2_late_rally_min,
            recovery_drop_min=self.config.l2_late_recovery_drop,
            recovery_rise_min=self.config.l2_late_recovery_rise,
            active_buy_ratio_min=self.config.l2_active_buy_ratio_min,
            require_capital=False,
            require_orderbook=False,
        )
        self.l3_config = L3Config(
            require_above_ma=False,
            min_history_win_rate=self.config.l3_min_history_win_rate,
            max_volatility=self.config.l3_max_volatility,
            max_consecutive_limits=self.config.l3_max_consecutive_limits,
        )
        self.l4_config = L4Config()
        self.kline_config = KlineConfig(
            min_atr_pct=self.config.kline_min_atr_pct,
            max_consecutive_up=self.config.kline_max_consecutive_up,
            min_yang_body_pct=self.config.kline_min_yang_body_pct,
        )

    def run(self) -> dict:
        """执行回测"""
        t0 = time.time()

        # 1. 加载股票池
        self._init_stock_pool()

        # 2. 获取交易日列表
        trading_days = self.loader.get_trading_days(self.config.start_date, self.config.end_date)
        logger.info(f"回测区间: {self.config.start_date} → {self.config.end_date}, {len(trading_days)} 个交易日")

        # 3. 预加载日线数据 (全部候选股，整段缓存)
        logger.info(f"预加载日线数据: {len(self.all_candidate_codes)} 只候选股")
        self.daily_bars = self.loader.load_daily_bars(
            self.all_candidate_codes,
            self.config.start_date,
            self.config.end_date,
        )

        # 4. 预加载北向数据
        self.northbound_data = self.loader.load_northbound_history(
            self.config.start_date,
            self.config.end_date,
        )

        # 5. 逐日循环
        for i, day in enumerate(trading_days):
            next_day = trading_days[i + 1] if i + 1 < len(trading_days) else None
            try:
                day_result = self._run_single_day(day, next_day)
                self.trade_log.record_day(day_result)
                n_trades = len(day_result.trades)
                if n_trades > 0 or day_result.buy_signals > 0:
                    logger.info(
                        f"[{day}] S1={day_result.s1_count} S2={day_result.s2_count} "
                        f"S3={day_result.s3_count} S4={day_result.s4_count} "
                        f"信号={day_result.buy_signals} 交易={n_trades}"
                    )
            except Exception as e:
                logger.error(f"[{day}] 回测异常: {e}", exc_info=True)

        # 6. 计算绩效
        metrics = PerformanceCalculator.calculate(self.trade_log.all_trades())

        # 7. 生成报告
        report_path = BacktestReportGenerator.generate(
            self.trade_log, metrics, self.config
        )

        elapsed = time.time() - t0
        logger.info(f"回测完成: {elapsed:.0f}s, {self.trade_log.total_days()}天, "
                     f"{len(self.trade_log.closed_trades())}笔交易")

        return {
            "metrics": metrics,
            "report_path": report_path,
            "total_days": self.trade_log.total_days(),
            "total_trades": len(self.trade_log.closed_trades()),
            "elapsed": elapsed,
        }

    # ============================================================
    # 股票池初始化
    # ============================================================

    def _init_stock_pool(self):
        """初始化候选股票池: 固定板块成分股"""
        sectors = self.config.target_sectors
        if not sectors:
            logger.warning("TARGET_SECTORS 为空，无法构建候选池")
            return

        logger.info(f"从 {len(sectors)} 个板块加载成分股: {sectors}")
        self.sector_codes = self.loader.load_sector_constituents(sectors)

        # 构建 stock→sector 映射
        for sector, codes in self.sector_codes.items():
            for code in codes:
                if code not in self.sector_map:
                    self.sector_map[code] = sector

        # 合并去重
        unique = set()
        for codes in self.sector_codes.values():
            unique.update(codes)
        self.all_candidate_codes = list(unique)
        logger.info(f"候选池: {len(self.all_candidate_codes)} 只 (来自 {len(self.sector_codes)} 个板块)")

    # ============================================================
    # 单日回测
    # ============================================================

    def _run_single_day(self, date_str: str, next_date: Optional[str]) -> DayResult:
        t0 = time.time()

        # 1. 获取当日日线快照
        snapshot = self.loader.get_daily_snapshot(self.daily_bars, date_str)
        if snapshot.empty:
            logger.debug(f"[{date_str}] 无日线数据")
            return DayResult(date=date_str)

        # 获取 pre_close 映射 (从前一日数据)
        pre_close_map = self._get_pre_close_map(date_str)

        # 2. 加载5分钟线 (仅对于需要进入S2的股票，先跑S1再决定)
        # 优化: 先用日线跑 S1 过滤，只对通过的股票拉5分钟线
        s1_contexts = self._run_s1_with_daily(snapshot, date_str, pre_close_map)
        s1_count = len(s1_contexts)

        # 3. S2: 对S1通过的股票拉5分钟线，精确计算S2指标
        s2_contexts = self._run_s2_with_5min(s1_contexts, date_str, pre_close_map)
        s2_count = len(s2_contexts)

        # 4. S3: 技术验证
        s3_contexts = self._run_s3(s2_contexts, date_str)
        s3_count = len(s3_contexts)

        # 5. S4: 评分
        top30 = self._run_s4(s3_contexts, date_str)
        s4_count = len(top30)

        # 6. 记录交易信号
        buys = [c for c in top30 if c.recommendation in ("strong_buy", "buy")]
        day_trades = self._calculate_trades(buys, date_str, next_date)

        elapsed = time.time() - t0
        return DayResult(
            date=date_str,
            trades=day_trades,
            total_screened=len(snapshot),
            s1_count=s1_count,
            s2_count=s2_count,
            s3_count=s3_count,
            s4_count=s4_count,
            buy_signals=len(buys),
            elapsed_seconds=elapsed,
        )

    def _get_pre_close_map(self, date_str: str) -> dict[str, float]:
        """获取前一日收盘价映射"""
        import pandas as pd
        trading_days = self.loader.get_trading_days(self.config.start_date, self.config.end_date)
        try:
            idx = trading_days.index(date_str)
            if idx > 0:
                prev_day = trading_days[idx - 1]
                prev_snapshot = self.loader.get_daily_snapshot(self.daily_bars, prev_day)
                if not prev_snapshot.empty:
                    close_map = {}
                    for _, row in prev_snapshot.iterrows():
                        code = str(row.get("code", "")).zfill(6)
                        close = row.get("close", row.get("收盘", 0))
                        close_map[code] = float(close) if pd.notna(close) else 0.0
                    return close_map
        except (ValueError, IndexError):
            pass
        return {}

    # ============================================================
    # S1: L1 + K线 (仅日线数据)
    # ============================================================

    def _run_s1_with_daily(self, snapshot: pd.DataFrame, date_str: str,
                           pre_close_map: dict[str, float]) -> list:
        """用日线数据创建 StockContext 并跑 S1"""
        adapter = HistoricalDataAdapter(
            self.config, self.sector_map, sector_perf={}, northbound={}
        )
        contexts = adapter.adapt_single_day(snapshot, date_str, bars_5min=None,
                                            pre_close_map=pre_close_map)

        # L1 准入
        contexts = screen_l1_access(contexts, self.l1_config)
        # K线形态
        contexts = screen_kline(contexts, self.kline_config)

        # 过滤: L1通过 AND K线通过
        passed = [c for c in contexts if c.l1_passed and c.kline_passed]
        return passed

    # ============================================================
    # S2: 尾盘异动 (5分钟线精确)
    # ============================================================

    def _run_s2_with_5min(self, contexts: list, date_str: str,
                          pre_close_map: dict[str, float]) -> list:
        """对S1通过的股票拉5分钟线，重建 StockContext 并跑 S2"""
        if not contexts:
            return []

        codes = [c.code for c in contexts]
        logger.info(f"[{date_str}] S2: 拉取 {len(codes)} 只5分钟线...")

        bars_5min = self.loader.load_5min_bars_batch(codes, date_str)
        logger.info(f"[{date_str}] S2: 获取到 {len(bars_5min)} 只5分钟线")

        # 重建 StockContext (带5分钟线精确S2指标)
        adapter = HistoricalDataAdapter(
            self.config, self.sector_map, sector_perf={}, northbound={}
        )
        # 重新获取日线快照
        snapshot = self.loader.get_daily_snapshot(self.daily_bars, date_str)
        mask = snapshot["code"].isin(codes) if "code" in snapshot.columns else pd.Series([False] * len(snapshot))
        snapshot_filtered = snapshot[mask] if mask.any() else snapshot

        # 重建所有S1通过的context (带精确S2指标)
        rebuilt = adapter.adapt_single_day(
            snapshot_filtered, date_str, bars_5min=bars_5min, pre_close_map=pre_close_map
        )
        # 重新应用 L1 + K线
        rebuilt = screen_l1_access(rebuilt, self.l1_config)
        rebuilt = screen_kline(rebuilt, self.kline_config)
        rebuilt = [c for c in rebuilt if c.l1_passed and c.kline_passed]

        # L2 异动检测 (使用精确S2指标)
        passed = screen_l2_anomaly(
            rebuilt, self.l2_config,
            has_depth_data=False,
            has_capital_data=(self.config.capital_flow_mode != "none"),
        )
        return passed

    # ============================================================
    # S3: 技术验证
    # ============================================================

    def _run_s3(self, contexts: list, date_str: str) -> list:
        """L3 技术验证"""
        if not contexts:
            return []
        passed = screen_l3_technical(contexts, preloader=None, config=self.l3_config)
        return passed

    # ============================================================
    # S4: 评分 + 排序
    # ============================================================

    def _run_s4(self, contexts: list, date_str: str) -> list:
        """L4 量化评分 + 排序"""
        if not contexts:
            return []

        # 设置北向情绪
        nb = self.northbound_data.get(date_str, {})
        if nb:
            set_northbound_sentiment(nb)

        # L4 评分
        scored = score_l4(contexts, self.l4_config)

        # 取 Top 30
        sorted_ctx = sorted(scored, key=lambda c: c.total_score, reverse=True)
        top30 = sorted_ctx[:self.l4_config.max_total_output]

        # 融合排序 (纯规则，rule_weight=1.0)
        merge_and_rank(top30, llm_results={}, rule_weight=1.0)

        return top30

    # ============================================================
    # 收益计算
    # ============================================================

    def _calculate_trades(self, buys: list, date_str: str,
                          next_date: Optional[str]) -> list[Trade]:
        """计算买入信号的 T+1 退出收益"""
        trades = []
        if not next_date:
            return trades

        next_snapshot = self.loader.get_daily_snapshot(self.daily_bars, next_date)
        if next_snapshot.empty:
            return trades

        # 建立次日开盘价索引
        next_open_map = {}
        for _, row in next_snapshot.iterrows():
            code = str(row.get("code", "")).zfill(6)
            open_price = float(row.get("open", row.get("开盘", 0)))
            if open_price > 0:
                next_open_map[code] = open_price

        for ctx in buys[:self.config.max_positions]:
            entry = ctx.price * (1 + self.config.slippage_bps / 10000)
            exit_price = next_open_map.get(ctx.code)
            if exit_price is None:
                continue

            exit_p = exit_price * (1 - self.config.slippage_bps / 10000)
            return_pct = (exit_p - entry) / entry * 100 - self.config.commission_rate * 2 * 100

            trades.append(Trade(
                date=date_str,
                code=ctx.code,
                name=ctx.name,
                entry_price=round(entry, 3),
                exit_price=round(exit_p, 3),
                return_pct=round(return_pct, 4),
                recommendation=ctx.recommendation,
                total_score=round(ctx.total_score, 2),
                anomaly_type=ctx.anomaly_type,
                sector=ctx.sector,
                score_tail=round(ctx.score_tail_strength, 2),
                score_tech=round(ctx.score_technical, 2),
                score_capital=round(ctx.score_capital, 2),
                score_env=round(ctx.score_market_env, 2),
                score_history=round(ctx.score_history, 2),
                score_fundamental=round(getattr(ctx, 'score_fundamental', 0), 2),
            ))

        return trades
