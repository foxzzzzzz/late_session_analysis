"""回测配置 — 扩展 SystemConfig，增加回测专属参数"""
import os
from dataclasses import dataclass, field
from orchestration.config import SystemConfig


@dataclass
class BacktestConfig(SystemConfig):
    """回测配置，继承实时管线 SystemConfig 的所有筛选阈值"""

    # 日期范围
    start_date: str = "20260101"
    end_date: str = "20260515"

    # 股票池: 固定板块（从 TARGET_SECTORS 继承），skip_s0=True 时直接使用成分股
    skip_s0: bool = True

    # 数据缓存
    cache_dir: str = "./backtest_cache"
    no_cache: bool = False

    # 5分钟线获取
    use_5min_data: bool = True
    max_5min_workers: int = 3
    rate_limit_per_sec: float = 2.0

    # 回测口径: historical=历史近似, live_replay=实盘快照回放
    backtest_type: str = "historical"
    decision_time: str = "14:57"
    live_snapshot_dir: str = "./live_snapshots"

    # 资金流模式: "none" | "proxy" | "replay"
    capital_flow_mode: str = "none"

    # 退出模拟
    entry_price_type: str = "close"
    exit_price_type: str = "next_open"
    slippage_bps: float = 5.0
    commission_rate: float = 0.00025

    # 风险控制
    stop_loss_pct: float = -5.0    # 止损线 (%) — 负值表示亏损
    take_profit_pct: float = 5.0   # 止盈线 (%) — 正值表示盈利

    # 仓位
    max_positions: int = 5

    # 输出
    output_dir: str = "./backtest_reports"

    def __post_init__(self):
        super().__post_init__()
        self._load_bt_from_env()

    def _load_bt_from_env(self):
        self.start_date = os.getenv("BT_START_DATE", self.start_date)
        self.end_date = os.getenv("BT_END_DATE", self.end_date)
        self.skip_s0 = os.getenv("BT_SKIP_S0", str(self.skip_s0)).lower() in ("true", "1", "yes")
        self.cache_dir = os.getenv("BT_CACHE_DIR", self.cache_dir)
        self.no_cache = os.getenv("BT_NO_CACHE", "").lower() in ("true", "1", "yes")
        self.use_5min_data = os.getenv("BT_USE_5MIN", str(self.use_5min_data)).lower() in ("true", "1", "yes")
        self.backtest_type = os.getenv("BT_BACKTEST_TYPE", self.backtest_type)
        self.decision_time = os.getenv("BT_DECISION_TIME", self.decision_time)
        self.live_snapshot_dir = os.getenv("BT_LIVE_SNAPSHOT_DIR", self.live_snapshot_dir)
        self.capital_flow_mode = os.getenv("BT_CAPITAL_FLOW_MODE", self.capital_flow_mode)
        self.slippage_bps = float(os.getenv("BT_SLIPPAGE_BPS", str(self.slippage_bps)))
        self.commission_rate = float(os.getenv("BT_COMMISSION_RATE", str(self.commission_rate)))
        self.output_dir = os.getenv("BT_OUTPUT_DIR", self.output_dir)

        # 回测 L2 阈值 — 默认对齐实盘，可通过 BT_L2_* 环境变量覆盖
        self.l2_volume_ratio = float(os.getenv("BT_L2_VOLUME_RATIO", str(self.l2_volume_ratio)))
        self.l2_last5min_vol_pct = float(os.getenv("BT_L2_LAST5MIN_VOL_PCT", str(self.l2_last5min_vol_pct)))
        self.l2_late_rally_pct = float(os.getenv("BT_L2_LATE_RALLY_PCT", str(self.l2_late_rally_pct)))
        self.l2_recovery_drop = float(os.getenv("BT_L2_RECOVERY_DROP", str(self.l2_recovery_drop)))
        self.l2_recovery_rise = float(os.getenv("BT_L2_RECOVERY_RISE", str(self.l2_recovery_rise)))
        self.l2_active_buy_pct = float(os.getenv("BT_L2_ACTIVE_BUY_PCT", str(self.l2_active_buy_pct)))

        # 回测专用 L2 最低保障
        self.l2_min_pass = int(os.getenv("BT_L2_MIN_PASS", str(self.l2_min_pass)))

        # 回测 K线 阈值 — 默认对齐实盘，可通过 BT_KLINE_* 环境变量覆盖
        self.kline_min_atr_pct = float(os.getenv("BT_KLINE_MIN_ATR_PCT", str(self.kline_min_atr_pct)))
        self.kline_max_atr_pct = float(os.getenv("BT_KLINE_MAX_ATR_PCT", str(self.kline_max_atr_pct)))
        self.kline_max_consecutive_up = int(os.getenv("BT_KLINE_MAX_CONSECUTIVE_UP", str(self.kline_max_consecutive_up)))
        self.kline_max_up_in_9days = int(os.getenv("BT_KLINE_MAX_UP_IN_9DAYS", str(self.kline_max_up_in_9days)))
        self.kline_max_single_day_pct = float(os.getenv("BT_KLINE_MAX_SINGLE_DAY_PCT", str(self.kline_max_single_day_pct)))
        self.kline_min_yang_ratio_4d = float(os.getenv("BT_KLINE_MIN_YANG_RATIO_4D", "0.50"))
        self.kline_min_consecutive_close_rise = int(os.getenv("BT_KLINE_MIN_CONSECUTIVE_CLOSE_RISE", str(self.kline_min_consecutive_close_rise)))
        self.kline_min_close_rise_pct = float(os.getenv("BT_KLINE_MIN_CLOSE_RISE_PCT", str(self.kline_min_close_rise_pct)))

        # 回测 L4 推荐阈值 — 回测缺实盘资金流+LLM+盘口, 得分上限~50(中位数~30)
        # 回测专用阈值 35/25/15 (e1ec58f原始值), 不可与实盘85/75/60共用
        # 可通过 BT_L4_* 环境变量覆盖
        self.l4_high_threshold = float(os.getenv("BT_L4_HIGH_THRESHOLD", "35.0"))
        self.l4_buy_threshold = float(os.getenv("BT_L4_BUY_THRESHOLD", "25.0"))
        self.l4_medium_threshold = float(os.getenv("BT_L4_MEDIUM_THRESHOLD", "15.0"))

        # 修复regime覆盖字典中的L4条目 — SystemConfig的bull/bear覆盖用的是实盘值
        # (85/75/60), _regime_value()会优先返回覆盖值, 导致回测阈值被架空
        # 等比缩放到回测量表: bull=同中性(35/25/15), bear=略降(32/23/13)
        self._regime_overrides["bull"]["l4_high_threshold"] = self.l4_high_threshold
        self._regime_overrides["bull"]["l4_buy_threshold"] = self.l4_buy_threshold
        self._regime_overrides["bull"]["l4_medium_threshold"] = self.l4_medium_threshold
        self._regime_overrides["bear"]["l4_high_threshold"] = 32.0
        self._regime_overrides["bear"]["l4_buy_threshold"] = 23.0
        self._regime_overrides["bear"]["l4_medium_threshold"] = 13.0

        # 回测 L3 波动率 — 继承实盘值 (默认0.60), 可通过 BT_L3_MAX_VOLATILITY 独立覆盖
        self.l3_max_volatility = float(os.getenv("BT_L3_MAX_VOLATILITY", str(self.l3_max_volatility)))
