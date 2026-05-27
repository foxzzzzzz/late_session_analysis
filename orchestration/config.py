"""系统配置管理 — YAML + .env"""
import json
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

    l2_volume_ratio: float = 1.2       # 尾盘量比 (14:30后 / 13:00-14:30)
    l2_last5min_vol_pct: float = 5.0   # 最后5分钟量占比(%)
    l2_late_rally_pct: float = 2.0     # 尾盘拉升最低涨幅(%)
    l2_recovery_drop: float = 3.0
    l2_recovery_rise: float = 1.5
    l2_active_buy_pct: float = 55.0
    l2_require_capital: bool = True    # 资金流向是否作为硬门槛 (false=仅评分使用)
    l2_min_pass: int = 10              # L2 最低通过数，不足时自动放宽资金条件
    l2_big_order_net_min: float = 0    # 大单净流入下限(万元)
    l2_big_order_ratio_mult: float = 1.3  # 尾盘大单占比 / 全天平均
    l2_cancel_rate_max: float = 30.0   # 最大撤单率(%)
    l2_require_orderbook: bool = False  # 盘口数据作为硬门槛 (MVP阶段不启用)

    l3_sector_rank_top_pct: float = 30.0
    l3_min_history_win: float = 40.0  # 近5日收阳率最低要求(%)
    l3_max_volatility: float = 0.60   # 最大波动率(20日标准差, 小数)
    l3_vol_ratio_min: float = 1.1       # 量比下限
    l3_vol_ratio_max: float = 2.0       # 量比上限
    l3_ma5_close_ratio_min: float = 1.0  # 收盘/MA5 最低比率
    l3_ma5_low_ratio_min: float = 0.98   # 最低价/MA5 最低比率

    l4_high_threshold: float = 85.0    # strong_buy (超强信号, 策略 >85)
    l4_medium_threshold: float = 60.0   # watch 下限
    l4_buy_threshold: float = 75.0      # buy (强信号, 策略 75-85)
    l4_max_high_attention: int = 15     # strong_buy 最大输出数
    l4_max_total_output: int = 30       # 最大输出总数

    # === L4 A维度: 尾盘强度 (max 30) ===
    l4_late_price_tiers: str = '[[4.0,8],[2.0,6],[1.0,4],[0.0,2]]'           # 尾盘涨幅阶梯 [阈值,分数]
    l4_vol_ratio_tiers: str = '[[3.0,8],[2.0,6],[1.5,4],[1.0,2]]'            # 尾盘放量倍数阶梯
    l4_last5min_bonus_tiers: str = '[[15.0,1.0],[10.0,0.5]]'                 # 最后5分钟量占比加成
    l4_big_order_tiers: str = '[[0.3,8],[0.2,6],[0.1,4]]'                    # 大单占比阶梯
    l4_big_order_net_score: float = 2.0                                       # 大单净流入>0基础分
    l4_bid_ask_tiers: str = '[[2.0,6],[1.5,4],[1.0,2]]'                      # 买卖挂单比阶梯

    # === L4 B维度: K线形态 (max 25) ===
    l4_yang_days_tiers: str = '[[4,8],[3,6],[2,4],[1,2]]'                    # 近4天阳线天数阶梯
    l4_close_rise_tiers: str = '[[4,7],[3,5],[2,3],[1,1]]'                   # 连续收盘上涨天数阶梯
    l4_volatility_penalty_tiers: str = '[[0.40,-2],[0.30,-1]]'               # 波动率惩罚阶梯 [阈值(小数,如0.30=30%),扣分]
    l4_body_amplifying_score: float = 5.0                                     # 实体放大得分
    l4_yang_no_amplify_score: float = 2.0                                     # 阳线多但未放大得分
    l4_broke_high_score: float = 5.0                                          # 突破前高得分
    l4_breakout_score: float = 3.0                                            # 异动类型=breakout得分

    # === L4 C维度: 资金面 (max 20) ===
    l4_flow_net_tiers: str = '[[1000,10],[500,7],[100,4]]'                   # 主力净流入阶梯(万元)
    l4_flow_net_positive_score: float = 2.0                                   # 主力净流入>0基础分
    l4_flow_ratio_score: float = 5.0                                          # 大单占比>=0.2加分
    l4_active_buy_tiers: str = '[[60,5],[55,3]]'                              # 主动买入占比阶梯
    l4_northbound_tiers: str = '[[70,3],[55,1]]'                              # 北向情绪阶梯

    # === L4 D维度: 均线系统 (max 15) ===
    l4_ma_alignment_scores: str = '{"bullish":8,"above_ma5":5,"bottom_area":3,"low_above_ma5":2}'  # 均线排列评分
    l4_ma5_accel_score: float = 4.0                                           # MA5加速得分
    l4_price_ma5_tiers: str = '[[1.01,3],[1.005,2],[1.0,1]]'                 # 收盘/MA5比率阶梯

    # === L4 E维度: 市场环境 (max 10) ===
    l4_sector_perf_tiers: str = '[[3,6],[1,4],[0,2],[-1,1]]'                 # 板块涨跌幅阶梯
    l4_concept_weight: float = 0.4                                            # 概念分权重
    l4_concept_max: float = 4.0                                               # 概念分上限(有analyzer)
    l4_hot_concept_per_item: float = 1.5                                      # 每概念基础分(无analyzer)
    l4_hot_concept_max: float = 4.0                                           # 概念分上限(无analyzer)
    l4_leader_bonus: float = 1.0                                              # 龙头效应附加分

    # === Rule Scorer (LLM降级规则评分) ===
    rule_late_price_tiers: str = '[[4,3],[2,2],[1,1]]'                        # 尾盘涨幅信号 [阈值,权重]
    rule_vol_ratio_tiers: str = '[[2.5,3],[1.5,2],[1.0,1]]'                  # 量比信号
    rule_big_order_tiers: str = '[[0.3,3]]'                                   # 大单信号 (ratio>=0.3且net>0→3分)
    rule_big_order_net_score: float = 1.0                                     # 大单净流入>0基础分
    rule_ma_bullish_score: float = 2.0                                        # 多头排列得分
    rule_ma_good_score: float = 1.0                                           # 技术面良好得分
    rule_decision_tiers: str = '[[8,"buy","A"],[5,"buy","B"],[3,"hold","B"]]'  # 决策阶梯 [最低分,决策,置信度]
    rule_default_decision: str = '["skip","C"]'                               # 默认决策 [决策,置信度]

    # === Pipeline 时间窗口 ===
    s2_window_end: str = "14:55"   # S2 尾盘异常扫描截止
    s3_window_end: str = "14:57"   # S3 技术面验证截止
    s4_window_end: str = "14:58"   # S4 融合评分截止

    # === 报告 ===
    report_output_dir: str = "./reports"
    enable_md_to_image: bool = False

    # === 并行 ===
    max_data_workers: int = 3

    # === 资金流向 (新浪 + 东财降级) ===
    enable_capital_flow: bool = True
    max_capital_enrich: int = 300  # S3资金流向富化上限，控制API调用量和时效性

    # === 北向资金情绪 ===
    enable_northbound: bool = True

    # === S0 板块预筛选 ===
    s0_top_n: int = 3
    s0_max_n: int = 5
    s0_min_stocks: int = 200

    # === S1 K线形态预筛选 ===
    kline_min_atr_pct: float = 2.0         # ATR/Close 最低(%)
    kline_max_atr_pct: float = 8.5         # ATR/Close 最高(%)
    kline_max_consecutive_up: int = 5      # 最多连涨天数
    kline_max_up_in_9days: int = 6         # 近9天最多涨几天
    kline_max_single_day_pct: float = 6.5  # 单日涨幅上限(%)
    kline_min_yang_ratio_4d: float = 0.75  # 近4天阳线占比最低 (3/4)
    kline_min_consecutive_close_rise: int = 3  # 至少连续N天收盘上涨
    kline_min_close_rise_pct: float = 0.3  # 连续上涨每天最低涨幅(%)
    kline_max_atr_multiple: float = 2.0    # 单日涨幅 <= N倍ATR
    kline_max_drop_ratio: float = 0.5      # R2: 涨幅骤降判定 (后一天/前一天 < 0.5)
    kline_max_consecutive_decline: int = 3  # R2: 连续递减天数阈值
    kline_max_consecutive_body_shrink: int = 3  # R2: 阳线实体连续缩小天数阈值
    kline_max_upper_shadow_ratio: float = 0.6  # R2: 上影线占实体比上限

    # === 时间循环间隔 (秒) ===
    s1_loop_interval: int = 180   # S1 K线扫描: 每3分钟
    s2_loop_interval: int = 60    # S2 尾盘异常: 每1分钟
    s3_loop_interval: int = 30    # S3 均线验证: 每30秒
    s4_loop_interval: int = 10    # S4 融合评分: 每10秒

    # === 调度 ===
    schedule_enabled: bool = False
    schedule_time: str = "14:25"

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
        self.l2_require_capital = os.getenv("L2_REQUIRE_CAPITAL", str(self.l2_require_capital)).lower() != "false"
        self.l2_min_pass = int(os.getenv("L2_MIN_PASS", str(self.l2_min_pass)))
        self.l2_big_order_net_min = float(os.getenv("L2_BIG_ORDER_NET_MIN", str(self.l2_big_order_net_min)))
        self.l2_big_order_ratio_mult = float(os.getenv("L2_BIG_ORDER_RATIO_MULT", str(self.l2_big_order_ratio_mult)))
        self.l2_cancel_rate_max = float(os.getenv("L2_CANCEL_RATE_MAX", str(self.l2_cancel_rate_max)))
        self.l2_require_orderbook = os.getenv("L2_REQUIRE_ORDERBOOK", str(self.l2_require_orderbook)).lower() in ("true","1")

        self.l3_sector_rank_top_pct = float(os.getenv("L3_SECTOR_RANK_TOP", str(self.l3_sector_rank_top_pct)))
        self.l3_min_history_win = float(os.getenv("L3_HISTORY_WIN_RATE", str(self.l3_min_history_win)))
        self.l3_vol_ratio_min = float(os.getenv("L3_VOL_RATIO_MIN", str(self.l3_vol_ratio_min)))
        self.l3_vol_ratio_max = float(os.getenv("L3_VOL_RATIO_MAX", str(self.l3_vol_ratio_max)))
        self.l3_max_volatility = float(os.getenv("L3_MAX_VOLATILITY", str(self.l3_max_volatility)))
        self.l3_ma5_close_ratio_min = float(os.getenv("L3_MA5_CLOSE_RATIO_MIN", str(self.l3_ma5_close_ratio_min)))
        self.l3_ma5_low_ratio_min = float(os.getenv("L3_MA5_LOW_RATIO_MIN", str(self.l3_ma5_low_ratio_min)))

        report_dir = os.getenv("REPORT_OUTPUT_DIR", "")
        if report_dir:
            self.report_output_dir = report_dir

        self.enable_northbound = os.getenv("ENABLE_NORTHBOUND", "true").lower() != "false"
        self.max_capital_enrich = int(os.getenv("MAX_CAPITAL_ENRICH", str(self.max_capital_enrich)))

        # S0
        self.s0_top_n = int(os.getenv("S0_TOP_N", str(self.s0_top_n)))
        self.s0_max_n = int(os.getenv("S0_MAX_N", str(self.s0_max_n)))
        self.s0_min_stocks = int(os.getenv("S0_MIN_STOCKS", str(self.s0_min_stocks)))

        # S1 K线
        self.kline_min_atr_pct = float(os.getenv("KLINE_MIN_ATR_PCT", str(self.kline_min_atr_pct)))
        self.kline_max_atr_pct = float(os.getenv("KLINE_MAX_ATR_PCT", str(self.kline_max_atr_pct)))
        self.kline_max_consecutive_up = int(os.getenv("KLINE_MAX_CONSECUTIVE_UP", str(self.kline_max_consecutive_up)))
        self.kline_max_up_in_9days = int(os.getenv("KLINE_MAX_UP_IN_9DAYS", str(self.kline_max_up_in_9days)))
        self.kline_max_single_day_pct = float(os.getenv("KLINE_MAX_SINGLE_DAY_PCT", str(self.kline_max_single_day_pct)))
        self.kline_min_yang_ratio_4d = float(os.getenv("KLINE_MIN_YANG_RATIO_4D", str(self.kline_min_yang_ratio_4d)))
        self.kline_min_consecutive_close_rise = int(os.getenv("KLINE_MIN_CONSECUTIVE_CLOSE_RISE", str(self.kline_min_consecutive_close_rise)))
        self.kline_min_close_rise_pct = float(os.getenv("KLINE_MIN_CLOSE_RISE_PCT", str(self.kline_min_close_rise_pct)))
        self.kline_max_atr_multiple = float(os.getenv("KLINE_MAX_ATR_MULTIPLE", str(self.kline_max_atr_multiple)))
        self.kline_max_drop_ratio = float(os.getenv("KLINE_MAX_DROP_RATIO", str(self.kline_max_drop_ratio)))
        self.kline_max_consecutive_decline = int(os.getenv("KLINE_MAX_CONSECUTIVE_DECLINE", str(self.kline_max_consecutive_decline)))
        self.kline_max_consecutive_body_shrink = int(os.getenv("KLINE_MAX_CONSECUTIVE_BODY_SHRINK", str(self.kline_max_consecutive_body_shrink)))
        self.kline_max_upper_shadow_ratio = float(os.getenv("KLINE_MAX_UPPER_SHADOW_RATIO", str(self.kline_max_upper_shadow_ratio)))

        # L4 tier fields (JSON string → parse → re-serialize to normalize)
        for attr in [a for a in dir(self) if a.startswith("l4_") and a.endswith("_tiers")]:
            env_key = attr.upper()
            raw = os.getenv(env_key, "")
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, attr, json.dumps(parsed))
                except json.JSONDecodeError:
                    pass
        for attr in ["l4_ma_alignment_scores"]:
            env_key = attr.upper()
            raw = os.getenv(env_key, "")
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, attr, json.dumps(parsed))
                except json.JSONDecodeError:
                    pass
        self.l4_big_order_net_score = float(os.getenv("L4_BIG_ORDER_NET_SCORE", str(self.l4_big_order_net_score)))
        self.l4_body_amplifying_score = float(os.getenv("L4_BODY_AMPLIFYING_SCORE", str(self.l4_body_amplifying_score)))
        self.l4_yang_no_amplify_score = float(os.getenv("L4_YANG_NO_AMPLIFY_SCORE", str(self.l4_yang_no_amplify_score)))
        self.l4_broke_high_score = float(os.getenv("L4_BROKE_HIGH_SCORE", str(self.l4_broke_high_score)))
        self.l4_breakout_score = float(os.getenv("L4_BREAKOUT_SCORE", str(self.l4_breakout_score)))
        self.l4_flow_net_positive_score = float(os.getenv("L4_FLOW_NET_POSITIVE_SCORE", str(self.l4_flow_net_positive_score)))
        self.l4_flow_ratio_score = float(os.getenv("L4_FLOW_RATIO_SCORE", str(self.l4_flow_ratio_score)))
        self.l4_ma5_accel_score = float(os.getenv("L4_MA5_ACCEL_SCORE", str(self.l4_ma5_accel_score)))
        self.l4_concept_weight = float(os.getenv("L4_CONCEPT_WEIGHT", str(self.l4_concept_weight)))
        self.l4_concept_max = float(os.getenv("L4_CONCEPT_MAX", str(self.l4_concept_max)))
        self.l4_hot_concept_per_item = float(os.getenv("L4_HOT_CONCEPT_PER_ITEM", str(self.l4_hot_concept_per_item)))
        self.l4_hot_concept_max = float(os.getenv("L4_HOT_CONCEPT_MAX", str(self.l4_hot_concept_max)))
        self.l4_leader_bonus = float(os.getenv("L4_LEADER_BONUS", str(self.l4_leader_bonus)))

        # Rule scorer
        for attr in [a for a in dir(self) if a.startswith("rule_") and a.endswith("_tiers")]:
            env_key = attr.upper()
            raw = os.getenv(env_key, "")
            if raw:
                try:
                    parsed = json.loads(raw)
                    setattr(self, attr, json.dumps(parsed))
                except json.JSONDecodeError:
                    pass
        self.rule_big_order_net_score = float(os.getenv("RULE_BIG_ORDER_NET_SCORE", str(self.rule_big_order_net_score)))
        self.rule_ma_bullish_score = float(os.getenv("RULE_MA_BULLISH_SCORE", str(self.rule_ma_bullish_score)))
        self.rule_ma_good_score = float(os.getenv("RULE_MA_GOOD_SCORE", str(self.rule_ma_good_score)))
        raw_rule_def = os.getenv("RULE_DEFAULT_DECISION", "")
        if raw_rule_def:
            try:
                self.rule_default_decision = json.dumps(json.loads(raw_rule_def))
            except json.JSONDecodeError:
                pass

        # Pipeline time windows
        self.s2_window_end = os.getenv("S2_WINDOW_END", self.s2_window_end)
        self.s3_window_end = os.getenv("S3_WINDOW_END", self.s3_window_end)
        self.s4_window_end = os.getenv("S4_WINDOW_END", self.s4_window_end)

        # 时间循环
        self.s1_loop_interval = int(os.getenv("S1_LOOP_INTERVAL", str(self.s1_loop_interval)))
        self.s2_loop_interval = int(os.getenv("S2_LOOP_INTERVAL", str(self.s2_loop_interval)))
        self.s3_loop_interval = int(os.getenv("S3_LOOP_INTERVAL", str(self.s3_loop_interval)))
        self.s4_loop_interval = int(os.getenv("S4_LOOP_INTERVAL", str(self.s4_loop_interval)))

    def need_llm(self) -> bool:
        """是否配置了LLM"""
        return bool(self.llm_api_key)

    def get_screening_configs(self):
        """导出为各层配置对象"""
        from screening.layer1_access import L1Config
        from screening.layer2_anomaly import L2Config
        from screening.layer3_technical import L3Config
        from screening.layer4_scoring import L4Config
        from screening.layer_kline import KlineConfig
        from analysis.rule_scorer import RuleScorerConfig

        return {
            'l1': L1Config(
                min_turnover=self.l1_min_turnover,
                min_turnover_rate=self.l1_min_turnover_rate,
                min_price=self.l1_min_price,
                max_price=self.l1_max_price,
            ),
            'kline': KlineConfig(
                min_atr_pct=self.kline_min_atr_pct,
                max_atr_pct=self.kline_max_atr_pct,
                max_consecutive_up=self.kline_max_consecutive_up,
                max_up_in_9days=self.kline_max_up_in_9days,
                max_single_day_pct=self.kline_max_single_day_pct,
                min_yang_ratio_4d=self.kline_min_yang_ratio_4d,
                min_consecutive_close_rise=self.kline_min_consecutive_close_rise,
                min_close_rise_pct=self.kline_min_close_rise_pct,
                max_atr_multiple=self.kline_max_atr_multiple,
                max_drop_ratio=self.kline_max_drop_ratio,
                max_consecutive_decline=self.kline_max_consecutive_decline,
                max_consecutive_body_shrink=self.kline_max_consecutive_body_shrink,
                max_upper_shadow_ratio=self.kline_max_upper_shadow_ratio,
            ),
            'l2': L2Config(
                volume_ratio_min=self.l2_volume_ratio,
                last_5min_vol_pct_min=self.l2_last5min_vol_pct,
                late_rally_min=self.l2_late_rally_pct,
                recovery_drop_min=self.l2_recovery_drop,
                recovery_rise_min=self.l2_recovery_rise,
                active_buy_ratio_min=self.l2_active_buy_pct,
                require_capital=self.l2_require_capital,
                big_order_net_min=self.l2_big_order_net_min,
                big_order_ratio_mult=self.l2_big_order_ratio_mult,
                cancel_rate_max=self.l2_cancel_rate_max,
                require_orderbook=self.l2_require_orderbook,
            ),
            'l3': L3Config(
                sector_rank_top_pct=self.l3_sector_rank_top_pct,
                min_history_win_rate=self.l3_min_history_win,
                max_volatility=self.l3_max_volatility,
                vol_ratio_min=self.l3_vol_ratio_min,
                vol_ratio_max=self.l3_vol_ratio_max,
                ma5_close_ratio_min=self.l3_ma5_close_ratio_min,
                ma5_low_ratio_min=self.l3_ma5_low_ratio_min,
            ),
            'l4': L4Config(
                high_attention_threshold=self.l4_high_threshold,
                medium_attention_threshold=self.l4_medium_threshold,
                buy_threshold=self.l4_buy_threshold,
                max_high_attention=self.l4_max_high_attention,
                max_total_output=self.l4_max_total_output,
                late_price_tiers=json.loads(self.l4_late_price_tiers),
                vol_ratio_tiers=json.loads(self.l4_vol_ratio_tiers),
                last5min_bonus_tiers=json.loads(self.l4_last5min_bonus_tiers),
                big_order_tiers=json.loads(self.l4_big_order_tiers),
                big_order_net_score=self.l4_big_order_net_score,
                bid_ask_tiers=json.loads(self.l4_bid_ask_tiers),
                yang_days_tiers=json.loads(self.l4_yang_days_tiers),
                close_rise_tiers=json.loads(self.l4_close_rise_tiers),
                volatility_penalty_tiers=json.loads(self.l4_volatility_penalty_tiers),
                body_amplifying_score=self.l4_body_amplifying_score,
                yang_no_amplify_score=self.l4_yang_no_amplify_score,
                broke_high_score=self.l4_broke_high_score,
                breakout_score=self.l4_breakout_score,
                flow_net_tiers=json.loads(self.l4_flow_net_tiers),
                flow_net_positive_score=self.l4_flow_net_positive_score,
                flow_ratio_score=self.l4_flow_ratio_score,
                active_buy_tiers=json.loads(self.l4_active_buy_tiers),
                northbound_tiers=json.loads(self.l4_northbound_tiers),
                ma_alignment_scores=json.loads(self.l4_ma_alignment_scores),
                ma5_accel_score=self.l4_ma5_accel_score,
                price_ma5_tiers=json.loads(self.l4_price_ma5_tiers),
                sector_perf_tiers=json.loads(self.l4_sector_perf_tiers),
                concept_weight=self.l4_concept_weight,
                concept_max=self.l4_concept_max,
                hot_concept_per_item=self.l4_hot_concept_per_item,
                hot_concept_max=self.l4_hot_concept_max,
                leader_bonus=self.l4_leader_bonus,
            ),
            'rule_scorer': RuleScorerConfig(
                late_price_tiers=json.loads(self.rule_late_price_tiers),
                vol_ratio_tiers=json.loads(self.rule_vol_ratio_tiers),
                big_order_tiers=json.loads(self.rule_big_order_tiers),
                big_order_net_score=self.rule_big_order_net_score,
                ma_bullish_score=self.rule_ma_bullish_score,
                ma_good_score=self.rule_ma_good_score,
                decision_tiers=json.loads(self.rule_decision_tiers),
                default_decision=json.loads(self.rule_default_decision),
            ),
        }
