"""系统配置管理 — YAML + .env"""
import os
import logging
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SystemConfig:
    """尾盘分析系统全局配置"""

    # === 数据源 ===
    data_providers: list[str] = field(default_factory=lambda: ["tencent", "sector_based", "sina", "efinance", "akshare"])
    target_sectors: list[str] = field(default_factory=list)
    rate_limit_min_sleep: float = 1.5
    rate_limit_max_sleep: float = 3.0

    # === LLM ===
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_api_base: str = ""
    llm_max_tokens: int = 512
    llm_temperature: float = 0.3

    # === 筛选阈值 ===
    l1_min_turnover: float = 50_000_000
    l1_min_turnover_rate: float = 1.0
    l1_min_price: float = 5.0
    l1_max_price: float = 100.0

    l2_volume_ratio: float = 2.5       # 尾盘量比(相对上午), 1.5过于宽松 → 2.5
    l2_last5min_vol_pct: float = 12.0  # 最后5分钟量占比(%)
    l2_late_rally_pct: float = 3.0     # 尾盘拉升最低涨幅(%)
    l2_recovery_drop: float = 3.0
    l2_recovery_rise: float = 1.5
    l2_active_buy_pct: float = 55.0

    l3_sector_rank_top_pct: float = 30.0
    l3_min_history_win: float = 60.0

    l4_high_threshold: float = 75.0
    l4_medium_threshold: float = 60.0

    # === 报告 ===
    report_output_dir: str = "./reports"
    enable_md_to_image: bool = False

    # === 并行 ===
    max_data_workers: int = 3

    # === 百度资金流向 ===
    enable_capital_flow: bool = True
    max_capital_enrich: int = 300  # S3资金流向富化上限，控制API调用量和时效性

    # === 北向资金情绪 ===
    enable_northbound: bool = True

    # === 调度 ===
    schedule_enabled: bool = False
    schedule_time: str = "14:29"

    def __post_init__(self):
        self._load_from_env()

    def _load_from_env(self):
        """从环境变量加载配置"""
        # 数据源优先级
        dp_env = os.getenv("DATA_PROVIDER_PRIORITY", "")
        if dp_env:
            self.data_providers = [s.strip() for s in dp_env.split(",") if s.strip()]

        # 目标板块
        sectors_env = os.getenv("TARGET_SECTORS", "")
        if sectors_env:
            self.target_sectors = [s.strip() for s in sectors_env.split(",") if s.strip()]

        self.rate_limit_min_sleep = float(os.getenv("RATE_LIMIT_MIN_SLEEP", str(self.rate_limit_min_sleep)))
        self.rate_limit_max_sleep = float(os.getenv("RATE_LIMIT_MAX_SLEEP", str(self.rate_limit_max_sleep)))

        if not self.llm_provider:
            self.llm_provider = os.getenv("LLM_PROVIDER", "deepseek")
        if not self.llm_model:
            self.llm_model = os.getenv("LLM_MODEL", "deepseek-chat")
        if not self.llm_api_key:
            self.llm_api_key = os.getenv("LLM_API_KEY", "")
        if not self.llm_api_base:
            self.llm_api_base = os.getenv("LLM_API_BASE", "")

        self.llm_max_tokens = int(os.getenv("LLM_MAX_TOKENS", str(self.llm_max_tokens)))
        self.llm_temperature = float(os.getenv("LLM_TEMPERATURE", str(self.llm_temperature)))

        self.l1_min_turnover = float(os.getenv("L1_MIN_TURNOVER", str(self.l1_min_turnover)))
        self.l1_min_turnover_rate = float(os.getenv("L1_MIN_TURNOVER_RATE", str(self.l1_min_turnover_rate)))
        self.l1_min_price = float(os.getenv("L1_MIN_PRICE", str(self.l1_min_price)))
        self.l1_max_price = float(os.getenv("L1_MAX_PRICE", str(self.l1_max_price)))

        self.l2_volume_ratio = float(os.getenv("L2_VOLUME_RATIO", str(self.l2_volume_ratio)))
        self.l2_last5min_vol_pct = float(os.getenv("L2_LAST5MIN_VOLUME_PCT", str(self.l2_last5min_vol_pct)))
        self.l2_late_rally_pct = float(os.getenv("L2_LATE_RALLY_PCT", str(self.l2_late_rally_pct)))
        self.l2_active_buy_pct = float(os.getenv("L2_ACTIVE_BUY_PCT", str(self.l2_active_buy_pct)))
        self.l2_recovery_drop = float(os.getenv("L2_LATE_RECOVERY_DROP", str(self.l2_recovery_drop)))
        self.l2_recovery_rise = float(os.getenv("L2_LATE_RECOVERY_RISE", str(self.l2_recovery_rise)))

        self.l3_sector_rank_top_pct = float(os.getenv("L3_SECTOR_RANK_TOP", str(self.l3_sector_rank_top_pct)))
        self.l3_min_history_win = float(os.getenv("L3_HISTORY_WIN_RATE", str(self.l3_min_history_win)))

        report_dir = os.getenv("REPORT_OUTPUT_DIR", "")
        if report_dir:
            self.report_output_dir = report_dir

        self.enable_northbound = os.getenv("ENABLE_NORTHBOUND", "true").lower() != "false"
        self.max_capital_enrich = int(os.getenv("MAX_CAPITAL_ENRICH", str(self.max_capital_enrich)))

    def need_llm(self) -> bool:
        """是否配置了LLM"""
        return bool(self.llm_api_key)

    def get_screening_configs(self):
        """导出为各层配置对象"""
        from screening.layer1_access import L1Config
        from screening.layer2_anomaly import L2Config
        from screening.layer3_technical import L3Config
        from screening.layer4_scoring import L4Config

        return {
            'l1': L1Config(
                min_turnover=self.l1_min_turnover,
                min_turnover_rate=self.l1_min_turnover_rate,
                min_price=self.l1_min_price,
                max_price=self.l1_max_price,
            ),
            'l2': L2Config(
                volume_ratio_min=self.l2_volume_ratio,
                last_5min_vol_pct_min=self.l2_last5min_vol_pct,
                late_rally_min=self.l2_late_rally_pct,
                recovery_drop_min=self.l2_recovery_drop,
                recovery_rise_min=self.l2_recovery_rise,
                active_buy_ratio_min=self.l2_active_buy_pct,
            ),
            'l3': L3Config(
                sector_rank_top_pct=self.l3_sector_rank_top_pct,
                min_history_win_rate=self.l3_min_history_win,
            ),
            'l4': L4Config(
                high_attention_threshold=self.l4_high_threshold,
                medium_attention_threshold=self.l4_medium_threshold,
            ),
        }
