"""S0 板块预筛选 — 从 .env 配置板块直接匹配成分股 → 候选股票池

数据源:
  1. baostock CSRC行业分类缓存 (data/sector_constituents_cache.json) — 主力
  2. akshare 东财成分股API (ak.stock_board_industry_cons_em) — 补充(交易时段可能拥堵)
"""
import json
import logging
import os
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)

# baostock 行业缓存路径
_BAOSTOCK_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    'data', 'sector_constituents_cache.json'
)

# .env板块名 → CSRC行业名 关键词匹配表
# 格式: (.env板块关键词, [CSRC行业名关键词列表])
_ENV_TO_CSRC_KEYWORDS = [
    ('影院', ['电影', '广播', '电视', '录音']),
    ('传媒', ['新闻', '出版', '文化', '广播', '电视']),
    ('IT', ['软件', '信息技术', '互联网']),
    ('软件', ['软件', '信息技术', '互联网']),
    ('通信', ['电信', '通信', '电子设备']),
    ('家电', ['电气机械', '器材制造', '电子设备']),
    ('医药', ['医药', '制药', '医疗']),
    ('医疗', ['医药', '制药', '医疗', '卫生']),
    ('食品', ['食品', '饮料', '酒']),
    ('白酒', ['酒']),
    ('银行', ['货币金融', '银行', '金融']),
    ('证券', ['资本市场', '证券']),
    ('保险', ['保险']),
    ('地产', ['房地产']),
    ('汽车', ['汽车', '铁路', '船舶', '航空航天']),
    ('新能源', ['电气机械', '能源', '电力']),
    ('半导体', ['电子设备', '半导体', '集成电路']),
    ('芯片', ['电子设备', '半导体', '集成电路']),
    ('化工', ['化学', '化工', '橡胶', '塑料']),
    ('钢铁', ['黑色金属', '有色金属', '金属制品']),
    ('有色', ['有色金属', '金属制品']),
    ('军工', ['航空航天', '船舶', '武器']),
    ('农业', ['农业', '林业', '牧业', '渔业']),
    ('旅游', ['住宿', '餐饮', '旅游']),
    ('建筑', ['建筑', '土木工程', '装修']),
    ('电力', ['电力', '能源', '燃气']),
    ('环保', ['生态', '环境', '水利']),
    ('教育', ['教育']),
    ('纺织', ['纺织', '服装', '皮革']),
    ('造纸', ['造纸', '印刷']),
    ('物流', ['运输', '物流', '仓储', '邮政']),
    ('电子元件', ['电子设备', '电子元件', '电子器件', '计算机']),
    ('计算机设备', ['计算机', '电子设备']),
    ('汽车零部件', ['汽车', '汽车零部件', '汽车配件']),
    ('软件开发', ['软件', '信息技术', '互联网']),
    ('消费电子', ['电子设备', '消费电子', '计算机']),
    ('光伏设备', ['电气机械', '能源', '电力', '光伏']),
]


class SectorFilter:
    """S0 板块预筛选：.env配置板块 → 匹配成分股 → 候选股票池

    数据源:
      1. baostock CSRC行业分类缓存 — 主力数据源
      2. akshare 东财成分股API — 补充(成功则合并去重)
    """

    def __init__(self, preloader=None, config=None):
        self.preloader = preloader
        self.config = config
        self.min_stocks: int = getattr(config, 's0_min_stocks', 200) if config else 200
        self.stock_sector_map: dict[str, str] = {}
        self._baostock_cache: Optional[dict] = None

    def filter(self, max_sectors: int = None) -> tuple[list[str], dict[str, str]]:
        """执行 S0 预筛选

        从 .env TARGET_SECTORS 读取目标板块 → 匹配成分股 → 合并去重

        Args:
            max_sectors: 限制板块数 (熊市扩展用)。若设置且preloader有行业表现数据,
                         则按涨幅取top-N行业, 忽略.env固定配置。
        Returns:
            (候选股票代码列表, 股票代码→板块名称映射)
        """
        t0 = time.time()

        # 1. 确定目标板块
        if max_sectors and self.preloader and self.preloader.sector_performance:
            # 动态选取: 按行业涨幅排名取 top-N
            ranked = sorted(
                self.preloader.sector_performance.items(),
                key=lambda x: x[1], reverse=True,
            )
            sectors = [name for name, _ in ranked[:max_sectors]]
            logger.info(
                f"S0 动态板块 (top-{max_sectors} 涨幅): {sectors[:5]}..."
            )
        else:
            if max_sectors:
                reason = (
                    f"preloader={self.preloader is not None}, "
                    f"perf_empty={not self.preloader.sector_performance if self.preloader else 'N/A'}"
                )
                logger.warning(f"S0 动态板块跳过 (max_sectors={max_sectors}, {reason}), 回退固定板块")
            sectors = self.config.target_sectors if self.config else []

            # 熊市扩展回退: 当 sector_performance 为空时，用所有关键词板块替代固定5板块
            if max_sectors and sectors:
                all_sectors = list(dict.fromkeys(kw for kw, _ in _ENV_TO_CSRC_KEYWORDS))
                sectors = all_sectors
                logger.warning(
                    f"S0 熊市回退: 因缺少行业行情数据，扩展至全部 {len(sectors)} 个关键词板块 "
                    f"(规避固定板块候选不足问题)"
                )

        if not sectors:
            logger.error("S0: 未配置 TARGET_SECTORS，无法预筛选")
            return [], {}

        logger.info(f"S0 目标板块 ({len(sectors)}个): {sectors[:5]}..."
                     if len(sectors) > 5 else f"S0 目标板块 ({len(sectors)}个): {sectors}")

        # 2. baostock 缓存匹配 (主力，必有)
        cache_stocks = self._get_stocks_from_cache(sectors)

        # 3. akshare 成分股API (补充，成功则合并)
        # 当baostock已返回足够股票时跳过，避免交易时段API拥堵
        if len(cache_stocks) >= self.min_stocks:
            akshare_stocks = []
            logger.debug(
                f"S0 跳过akshare补充 (baostock已有{len(cache_stocks)}只 >= {self.min_stocks})"
            )
        else:
            akshare_stocks = self._get_constituent_stocks_via_akshare(sectors)

        # 4. 合并去重
        merged: dict[str, str] = {}
        for code in cache_stocks:
            merged[code] = self.stock_sector_map.get(code, '')
        for code in akshare_stocks:
            if code not in merged:
                merged[code] = self.stock_sector_map.get(code, '')

        self.stock_sector_map = merged
        codes = list(merged.keys())

        elapsed = time.time() - t0
        logger.info(
            f"S0 完成 ({elapsed:.1f}s): "
            f"{len(codes)} 只候选股票 (baostock: {len(cache_stocks)}, "
            f"akshare: {len(akshare_stocks)}, 合并: {len(codes)})"
        )
        return codes, self.stock_sector_map

    # === baostock CSRC行业缓存 ===

    def _load_baostock_cache(self) -> dict[str, list[str]]:
        """加载 baostock CSRC行业→股票映射缓存"""
        if self._baostock_cache is not None:
            return self._baostock_cache
        try:
            if os.path.exists(_BAOSTOCK_CACHE_PATH):
                with open(_BAOSTOCK_CACHE_PATH, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                self._baostock_cache = raw.get('industries', {})
                logger.debug(
                    f"baostock缓存加载: {len(self._baostock_cache)} 个行业, "
                    f"{sum(len(v) for v in self._baostock_cache.values())} 只股票"
                )
            else:
                logger.warning(f"baostock缓存不存在: {_BAOSTOCK_CACHE_PATH}")
                self._baostock_cache = {}
        except Exception as e:
            logger.warning(f"baostock缓存加载失败: {e}")
            self._baostock_cache = {}
        return self._baostock_cache

    def _match_csrc_industries(self, env_sector: str) -> list[str]:
        """根据.env板块名匹配CSRC行业名

        匹配策略:
          1. 关键词匹配 (预定义映射表)
          2. 字符重叠度匹配 (兜底: 2字及以上重叠)
        """
        cache = self._load_baostock_cache()
        if not cache:
            return []

        matched = []

        # 策略1: 关键词匹配
        for env_kw, csrc_kws in _ENV_TO_CSRC_KEYWORDS:
            if env_kw in env_sector:
                for csrc_name in cache:
                    if any(kw in csrc_name for kw in csrc_kws):
                        if csrc_name not in matched:
                            matched.append(csrc_name)

        if matched:
            logger.debug(
                f"baostock匹配 [{env_sector}]: "
                f"{len(matched)} 个CSRC行业"
            )
            return matched

        # 策略2: 字符重叠度兜底 (2字+重叠)
        env_chars = set(env_sector.replace('行业', '').replace('板块', ''))
        for csrc_name in cache:
            csrc_short = csrc_name.split(' ', 1)[-1] if ' ' in csrc_name else csrc_name
            csrc_chars = set(csrc_short[:4])
            overlap = env_chars & csrc_chars
            if len(overlap) >= 2:
                matched.append(csrc_name)

        if matched:
            logger.debug(
                f"baostock模糊匹配 [{env_sector}]: "
                f"{len(matched)} 个CSRC行业"
            )

        return matched

    def _get_stocks_from_cache(self, sectors: list[str]) -> list[str]:
        """从baostock缓存获取板块成分股"""
        cache = self._load_baostock_cache()
        if not cache:
            return []

        seen: dict[str, str] = {}
        for sector in sectors:
            matched_industries = self._match_csrc_industries(sector)
            for ind in matched_industries:
                for code in cache.get(ind, []):
                    if code not in seen:
                        seen[code] = sector

        if seen:
            logger.info(
                f"S0 baostock匹配: {len(sectors)} 个板块 "
                f"→ {len(seen)} 只候选股票"
            )
        else:
            logger.warning(f"S0 未匹配到任何股票: 板块 {sectors}")

        self.stock_sector_map = seen
        return list(seen.keys())

    # === akshare 成分股补充 ===

    MAX_RETRIES = 1      # 单次尝试，交易时段东财大概率拥堵
    BASE_DELAY = 0.5     # 快速失败

    def _get_constituent_stocks_via_akshare(self, sectors: list[str]) -> list[str]:
        """通过 akshare 获取成分股 (补充数据源)"""
        try:
            import akshare as ak
        except ImportError:
            return []

        seen: dict[str, str] = {}
        for sector in sectors:
            df = None
            for attempt in range(self.MAX_RETRIES):
                try:
                    df = ak.stock_board_industry_cons_em(symbol=sector)
                    break
                except Exception as e:
                    if attempt < self.MAX_RETRIES - 1:
                        delay = self.BASE_DELAY + random.uniform(0, 0.3)
                        time.sleep(delay)
                    else:
                        logger.debug(f"S0 akshare [{sector}] 失败: {e}")

            if df is None or df.empty:
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
            if count:
                logger.debug(f"S0 akshare [{sector}]: {count} 只")

        if seen:
            logger.info(f"S0 akshare补充: {len(seen)} 只")
            # 不覆盖 stock_sector_map，akshare作为补充
            for code, sector in seen.items():
                if code not in self.stock_sector_map:
                    self.stock_sector_map[code] = sector

        return list(seen.keys())
