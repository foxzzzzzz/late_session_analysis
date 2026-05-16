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

    # 资金流模式: "none" | "estimated"
    capital_flow_mode: str = "none"

    # 退出模拟
    entry_price_type: str = "close"
    exit_price_type: str = "next_open"
    slippage_bps: float = 5.0
    commission_rate: float = 0.00025

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
        self.capital_flow_mode = os.getenv("BT_CAPITAL_FLOW_MODE", self.capital_flow_mode)
        self.slippage_bps = float(os.getenv("BT_SLIPPAGE_BPS", str(self.slippage_bps)))
        self.commission_rate = float(os.getenv("BT_COMMISSION_RATE", str(self.commission_rate)))
        self.output_dir = os.getenv("BT_OUTPUT_DIR", self.output_dir)

        # 回测专用 L2 阈值 — 比实盘管线宽松
        # 实盘在 14:50-14:55 每1分钟重复扫描，单次阈值高仍有多次机会
        # 回测用收盘快照做单次判断，必须调低阈值才能捕获足够候选
        # 每个阈值都可通过 BT_L2_* 环境变量覆盖
        self.l2_volume_ratio = float(os.getenv("BT_L2_VOLUME_RATIO", "1.5"))
        self.l2_last5min_vol_pct = float(os.getenv("BT_L2_LAST5MIN_VOL_PCT", "6.0"))
        self.l2_late_rally_pct = float(os.getenv("BT_L2_LATE_RALLY_PCT", "1.5"))
        self.l2_recovery_drop = float(os.getenv("BT_L2_RECOVERY_DROP", "2.0"))
        self.l2_recovery_rise = float(os.getenv("BT_L2_RECOVERY_RISE", "1.0"))
        self.l2_active_buy_pct = float(os.getenv("BT_L2_ACTIVE_BUY_PCT", "50.0"))

        # 回测专用 L4 推荐阈值 — 无资金流/LLM时评分偏低，需下调
        self.l4_strong_buy = float(os.getenv("BT_L4_STRONG_BUY", "35.0"))
        self.l4_buy = float(os.getenv("BT_L4_BUY", "25.0"))
        self.l4_watch = float(os.getenv("BT_L4_WATCH", "15.0"))
