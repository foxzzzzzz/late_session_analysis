"""回测引擎 — 逐交易日循环 S1→S2→S3→S4"""
import json
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

        # 预计算全部3种市场状态的筛选配置
        self._regime_configs: dict[str, dict] = {}
        for r in ["bull", "bear", "neutral"]:
            self._regime_configs[r] = self.config.get_screening_configs(regime=r)

        # 默认使用中性配置 (auto模式会在每日循环中动态切换)
        self._set_configs_for_regime("neutral")
        if self.config.regime_mode != "auto":
            self._set_configs_for_regime(self.config.regime_mode)
            logger.info(f"回测市场状态(固定): {self.config.regime_mode}")

        # 逐日regime (auto模式在run()中填充)
        self._day_regimes: dict[str, str] = {}

    def _set_configs_for_regime(self, regime: str):
        """将当前筛选配置切换到指定市场状态"""
        cfgs = self._regime_configs[regime]
        self.l1_config = cfgs["l1"]
        self.kline_config = cfgs["kline"]
        self.l2_config = cfgs["l2"]
        self.l3_config = cfgs["l3"]
        self.l4_config = cfgs["l4"]

    def run(self) -> dict:
        """执行回测"""
        t0 = time.time()

        # 1. 加载股票池
        self._init_stock_pool()

        # 2. 获取交易日列表
        trading_days = self.loader.get_trading_days(self.config.start_date, self.config.end_date)
        logger.info(f"回测区间: {self.config.start_date} → {self.config.end_date}, {len(trading_days)} 个交易日")

        # 2b. 加载上证指数日线 + 逐日判定市场状态 (auto模式)
        if self.config.regime_mode == "auto":
            sh_index_bars = self._load_sh_index_bars()
            if sh_index_bars is not None and not sh_index_bars.empty:
                self._detect_daily_regimes(trading_days, sh_index_bars)
                bull_days = sum(1 for d in trading_days if self._day_regimes.get(d) == "bull")
                bear_days = sum(1 for d in trading_days if self._day_regimes.get(d) == "bear")
                neutral_days = sum(1 for d in trading_days if self._day_regimes.get(d) == "neutral")
                logger.info(
                    f"市场状态分布: bull={bull_days}d bear={bear_days}d "
                    f"neutral={neutral_days}d"
                )
            else:
                logger.warning("上证指数数据不可用, auto模式降级为neutral")

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
                    regime_str = self._day_regimes.get(day, self.config.regime_mode)
                    logger.info(
                        f"[{day}] {regime_str} S1={day_result.s1_count} S2={day_result.s2_count} "
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
    # 市场状态判定 (回测逐日)
    # ============================================================

    def _load_sh_index_bars(self) -> "pd.DataFrame | None":
        """加载上证指数(999999)日线数据用于逐日市场状态判定"""
        try:
            import baostock as bs
            bs.login()
            try:
                fetch_start = f"{self.config.start_date[:4]}-{self.config.start_date[4:6]}-{self.config.start_date[6:8]}"
                fetch_end = f"{self.config.end_date[:4]}-{self.config.end_date[4:6]}-{self.config.end_date[6:8]}"
                # 多拉60天用于20日收益计算
                import pandas as pd
                extended_start = (pd.Timestamp(fetch_start) - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
                rs = bs.query_history_k_data_plus(
                    "sh.000001",
                    "date,close",
                    start_date=extended_start, end_date=fetch_end,
                    frequency="d", adjustflag="3",
                )
                if rs.error_code != "0":
                    logger.warning(f"上证指数查询失败: {rs.error_msg}")
                    return None
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return None
                df = pd.DataFrame(rows, columns=["date", "close"])
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                return df.dropna(subset=["close"])
            finally:
                bs.logout()
        except Exception as e:
            logger.warning(f"上证指数加载失败: {e}")
            return None

    def _detect_daily_regimes(self, trading_days: list[str], index_df: "pd.DataFrame"):
        """逐日判定市场状态, 含防抖 (按时间顺序, 模拟实盘)"""
        prev_state = {"regime": "neutral", "consecutive_days": 0}
        for day in trading_days:
            day_ts = pd.Timestamp(f"{day[:4]}-{day[4:6]}-{day[6:8]}")
            mask = pd.to_datetime(index_df["date"]) <= day_ts
            day_data = index_df[mask]
            if len(day_data) < 25:
                self._day_regimes[day] = "neutral"
                continue

            close = day_data["close"].dropna()
            if len(close) < 25:
                self._day_regimes[day] = "neutral"
                continue

            # 因子1: 20日收益率
            ret_20d = (float(close.iloc[-1]) - float(close.iloc[-21])) / float(close.iloc[-21]) * 100
            # 因子2: vs MA20
            ma20 = float(close.iloc[-20:].mean())
            above_ma20 = float(close.iloc[-1]) > ma20

            bull_score = 0
            if ret_20d >= 2:
                bull_score += 1
            elif ret_20d <= -2:
                bull_score -= 1
            if above_ma20:
                bull_score += 1
            else:
                bull_score -= 1

            if bull_score > 0:
                today_raw = "bull"
            elif bull_score < 0:
                today_raw = "bear"
            else:
                today_raw = "neutral"

            # 防抖: 连续2天同方向才切换, neutral立即生效
            if today_raw == prev_state["regime"]:
                final = today_raw
                prev_state["consecutive_days"] += 1
            elif prev_state["regime"] == "neutral" or today_raw == "neutral":
                final = today_raw
                prev_state = {"regime": today_raw, "consecutive_days": 1}
            else:
                final = prev_state["regime"]

            self._day_regimes[day] = final

    # ============================================================
    # 单日回测
    # ============================================================

    def _run_single_day(self, date_str: str, next_date: Optional[str]) -> DayResult:
        t0 = time.time()

        # 逐日regime切换 (auto模式)
        regime = self._day_regimes.get(date_str)
        if regime:
            self._set_configs_for_regime(regime)

        # 1. 获取当日日线快照
        snapshot = self.loader.get_daily_snapshot(self.daily_bars, date_str)
        if snapshot.empty:
            logger.debug(f"[{date_str}] 无日线数据")
            return DayResult(date=date_str)

        # 获取 pre_close 映射 (从前一日数据)
        pre_close_map = self._get_pre_close_map(date_str)

        # 计算板块涨跌幅 (回填 sector_performance 数据缺口)
        sector_perf = self._compute_sector_perf(snapshot)

        # 2. 加载5分钟线 (仅对于需要进入S2的股票，先跑S1再决定)
        # 优化: 先用日线跑 S1 过滤，只对通过的股票拉5分钟线
        s1_contexts = self._run_s1_with_daily(snapshot, date_str, pre_close_map, sector_perf)
        s1_count = len(s1_contexts)

        # 3. S2: 对S1通过的股票拉5分钟线，精确计算S2指标
        s2_contexts = self._run_s2_with_5min(s1_contexts, date_str, pre_close_map, sector_perf)
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

    def _compute_sector_perf(self, snapshot: pd.DataFrame) -> dict[str, float]:
        """从日线快照计算板块平均涨跌幅"""
        if not self.sector_codes or snapshot.empty:
            return {}
        sector_perf = {}
        for sector, codes in self.sector_codes.items():
            sector_rows = snapshot[snapshot["code"].isin(codes)]
            if not sector_rows.empty:
                chg = sector_rows.get("change_pct", sector_rows.get("涨跌幅"))
                if chg is not None and not chg.empty:
                    sector_perf[sector] = float(pd.to_numeric(chg, errors="coerce").mean())
        return sector_perf

    def _truncate_daily_bars(self, date_str: str) -> dict:
        """截断日线数据到指定日期 (避免 lookahead bias)"""
        result = {}
        for code, df in self.daily_bars.items():
            if df is None or df.empty:
                continue
            date_col = df.columns[0]
            mask = df[date_col].astype(str).str.replace("-", "").str[:8] <= date_str
            filtered = df[mask]
            if not filtered.empty:
                result[code] = filtered.reset_index(drop=True)
        return result

    # ============================================================
    # S1: L1 + K线 (仅日线数据)
    # ============================================================

    def _run_s1_with_daily(self, snapshot: pd.DataFrame, date_str: str,
                           pre_close_map: dict[str, float],
                           sector_perf: dict[str, float] = None) -> list:
        """用日线数据创建 StockContext 并跑 S1"""
        # 日线截断到当前回测日 (避免 lookahead bias)
        daily_upto = self._truncate_daily_bars(date_str)
        adapter = HistoricalDataAdapter(
            self.config, self.sector_map, sector_perf=sector_perf or {}, northbound={}
        )
        contexts = adapter.adapt_single_day(snapshot, date_str, bars_5min=None,
                                            pre_close_map=pre_close_map,
                                            daily_bars=daily_upto)

        # L1 准入
        all_contexts = contexts
        contexts = screen_l1_access(contexts, self.l1_config)

        # 诊断: 检查 L1 通过的 context 的 code 是否在 daily_upto 中
        l1_passed_codes = {c.code for c in contexts}
        daily_codes = set(daily_upto.keys())
        missing_from_daily = l1_passed_codes - daily_codes
        if missing_from_daily:
            logger.warning(
                f"[{date_str}] L1通过 {len(contexts)}只, 但 {len(missing_from_daily)}只 "
                f"不在 daily_upto 中! 样例: {list(missing_from_daily)[:5]}"
            )
        logger.debug(
            f"[{date_str}] S1诊断: total={len(all_contexts)} L1_pass={len(contexts)} "
            f"daily_keys={len(daily_codes)} missing={len(missing_from_daily)} "
            f"kline.yang={self.kline_config.min_yang_ratio_4d} "
            f"kline.atr=[{self.kline_config.min_atr_pct},{self.kline_config.max_atr_pct}]"
        )

        # K线形态 (传入截断后的日线数据)
        contexts = screen_kline(contexts, self.kline_config, daily_cache=daily_upto)

        # 过滤: L1通过 AND K线通过
        passed = [c for c in contexts if c.l1_passed and c.kline_passed]
        return passed

    # ============================================================
    # S2: 尾盘异动 (5分钟线精确)
    # ============================================================

    def _run_s2_with_5min(self, contexts: list, date_str: str,
                          pre_close_map: dict[str, float],
                          sector_perf: dict[str, float] = None) -> list:
        """对S1通过的股票拉5分钟线，重建 StockContext 并跑 S2"""
        if not contexts:
            return []

        codes = [c.code for c in contexts]
        logger.info(f"[{date_str}] S2: 拉取 {len(codes)} 只5分钟线...")

        bars_5min = self.loader.load_5min_bars_batch(codes, date_str)
        logger.info(f"[{date_str}] S2: 获取到 {len(bars_5min)} 只5分钟线")

        # 截断到14:59 — 模拟实盘收盘窗口，获取最完整的尾盘数据
        cutoff = pd.Timestamp(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} 14:59:00")
        truncated = {}
        for c, df in bars_5min.items():
            if isinstance(df, pd.DataFrame) and not df.empty:
                time_col = pd.to_datetime(df.iloc[:, 0])
                df_cut = df[time_col <= cutoff]
                if not df_cut.empty:
                    truncated[c] = df_cut
            else:
                truncated[c] = df
        bars_5min = truncated

        # 重建 StockContext (带5分钟线精确S2指标)
        adapter = HistoricalDataAdapter(
            self.config, self.sector_map, sector_perf=sector_perf or {}, northbound={}
        )
        # 重新获取日线快照
        snapshot = self.loader.get_daily_snapshot(self.daily_bars, date_str)
        mask = snapshot["code"].isin(codes) if "code" in snapshot.columns else pd.Series([False] * len(snapshot))
        snapshot_filtered = snapshot[mask] if mask.any() else snapshot

        # 重建所有S1通过的context (带精确S2指标)
        daily_upto = self._truncate_daily_bars(date_str)
        rebuilt = adapter.adapt_single_day(
            snapshot_filtered, date_str, bars_5min=bars_5min, pre_close_map=pre_close_map,
            daily_bars=daily_upto,
        )
        # 重新应用 L1 + K线
        rebuilt = screen_l1_access(rebuilt, self.l1_config)
        rebuilt = screen_kline(rebuilt, self.kline_config, daily_cache=daily_upto)
        rebuilt = [c for c in rebuilt if c.l1_passed and c.kline_passed]

        # L2 异动检测 (使用精确S2指标)
        has_capital = (self.config.capital_flow_mode != "none")
        passed = screen_l2_anomaly(
            rebuilt, self.l2_config,
            has_depth_data=False,
            has_capital_data=has_capital,
        )

        # 最低保障: 通过数不足时放宽资金条件重筛
        min_pass = self.config.l2_min_pass
        if len(passed) < min_pass and has_capital and self.l2_config.require_capital:
            import dataclasses
            relaxed = L2Config(**dataclasses.asdict(self.l2_config))
            relaxed.require_capital = False
            logger.warning(
                f"[{date_str}] L2 最低保障: 仅通过 {len(passed)} 只 (<{min_pass}), "
                f"放宽资金条件重筛 (量+价通过即可)"
            )
            passed = screen_l2_anomaly(
                rebuilt, relaxed,
                has_depth_data=False,
                has_capital_data=False,
            )
            logger.info(f"[{date_str}] L2 放宽后: {len(passed)} 只通过")

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

        # 诊断: 输出各维度得分分布
        if scored:
            scores = [c.total_score for c in scored]
            logger.info(
                f"[{date_str}] L4得分: min={min(scores):.0f} max={max(scores):.0f} "
                f"avg={sum(scores)/len(scores):.0f} n={len(scores)}"
            )
            for c in scored:
                logger.debug(
                    f"[{date_str}] L4 {c.code} {c.name}: "
                    f"A尾盘={c.score_tail_strength:.1f} B形态={c.score_technical:.1f} "
                    f"C资金={c.score_capital:.1f} D均线={c.score_ma_system:.1f} "
                    f"E环境={c.score_market_env:.1f} → {c.total_score:.1f}"
                )

        # 取 Top 30
        sorted_ctx = sorted(scored, key=lambda c: c.total_score, reverse=True)
        top30 = sorted_ctx[:self.l4_config.max_total_output]

        # 融合排序 (纯规则，rule_weight=1.0, 回测专用阈值)
        regime = self._day_regimes.get(date_str, self.config.regime_mode)
        if regime == "auto":
            regime = "neutral"
        merge_and_rank(
            top30, llm_results={}, rule_weight=1.0,
            strong_buy_threshold=self.config._regime_value("l4_strong_buy", regime),
            buy_threshold=self.config._regime_value("l4_buy", regime),
            watch_threshold=self.config._regime_value("l4_watch", regime),
        )

        return top30

    # ============================================================
    # 收益计算
    # ============================================================

    def _calculate_trades(self, buys: list, date_str: str,
                          next_date: Optional[str]) -> list[Trade]:
        """计算买入信号的 T+1 退出收益 (含止损/止盈风控)

        优先级: 止损 > 止盈 > 次日开盘
        止损: 次日最低价触及 entry * (1 + stop_loss_pct/100) → 以止损价退出
        止盈: 次日最高价触及 entry * (1 + take_profit_pct/100) → 以止盈价退出
        """
        trades = []
        if not next_date:
            return trades

        next_snapshot = self.loader.get_daily_snapshot(self.daily_bars, next_date)
        if next_snapshot.empty:
            return trades

        # 建立次日价格索引
        next_price_map = {}
        for _, row in next_snapshot.iterrows():
            code = str(row.get("code", "")).zfill(6)
            open_p = float(row.get("open", row.get("开盘", 0)))
            high_p = float(row.get("high", row.get("最高", 0)))
            low_p = float(row.get("low", row.get("最低", 0)))
            if open_p > 0:
                next_price_map[code] = {"open": open_p, "high": high_p, "low": low_p}

        stop_loss_pct = self.config.stop_loss_pct  # e.g. -5.0
        take_profit_pct = self.config.take_profit_pct  # e.g. 5.0

        for ctx in buys[:self.config.max_positions]:
            entry = ctx.price * (1 + self.config.slippage_bps / 10000)
            prices = next_price_map.get(ctx.code)
            if prices is None:
                continue

            stop_price = entry * (1 + stop_loss_pct / 100)
            profit_price = entry * (1 + take_profit_pct / 100)
            exit_p = None
            exit_reason = "next_open"

            # 止损 > 止盈 > 次日开盘
            if prices["low"] > 0 and prices["low"] <= stop_price:
                exit_p = stop_price * (1 - self.config.slippage_bps / 10000)
                exit_reason = "stop_loss"
            elif prices["high"] > 0 and prices["high"] >= profit_price:
                exit_p = profit_price * (1 - self.config.slippage_bps / 10000)
                exit_reason = "take_profit"
            else:
                exit_p = prices["open"] * (1 - self.config.slippage_bps / 10000)

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
                exit_reason=exit_reason,
            ))

        return trades
