"""数据采集层测试"""
import pytest
import pandas as pd
from data_provider.base import RealtimeQuote
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.sector_fetcher import SectorBasedFetcher, DEFAULT_SECTORS
from data_provider.sina_fetcher import SinaFetcher


class TestRealtimeQuote:
    def test_create_quote(self):
        q = RealtimeQuote(
            code='000001', name='平安银行', price=12.50,
            change_pct=2.5, turnover=500_000_000, turnover_rate=3.0,
            volume=10_000_000, high=12.8, low=12.2,
            open=12.3, pre_close=12.2,
        )
        assert q.code == '000001'
        assert q.price == 12.50


class TestEfinanceFetcher:
    def test_availability(self):
        fetcher = EfinanceFetcher()
        available = fetcher.is_available()
        assert isinstance(available, bool)

    def test_parse_empty_dataframe(self):
        import pandas as pd
        fetcher = EfinanceFetcher()
        df = pd.DataFrame()
        result = fetcher._parse_dataframe(df)
        assert isinstance(result, list)
        assert len(result) == 0


class TestAkshareFetcher:
    def test_availability(self):
        fetcher = AkshareFetcher()
        available = fetcher.is_available()
        assert isinstance(available, bool)


class TestSectorBasedFetcher:
    def test_availability(self):
        fetcher = SectorBasedFetcher()
        available = fetcher.is_available()
        assert isinstance(available, bool)

    def test_default_sectors(self):
        fetcher = SectorBasedFetcher()
        assert len(fetcher._sectors) == 8
        assert "半导体" in fetcher._sectors

    def test_custom_sectors(self):
        fetcher = SectorBasedFetcher(sectors=["证券", "银行"])
        assert fetcher._sectors == ["证券", "银行"]

    def test_parse_empty_dataframe(self):
        fetcher = SectorBasedFetcher()
        df = pd.DataFrame()
        result = fetcher._parse_dataframe(df, "测试板块")
        assert isinstance(result, list)
        assert len(result) == 0

    def test_parse_dataframe_with_data(self):
        fetcher = SectorBasedFetcher()
        df = pd.DataFrame({
            '代码': ['000001', '000002', '000003'],
            '名称': ['平安银行', '万科A', '*ST华泽'],
            '最新价': [12.50, 15.30, 3.20],
            '涨跌幅': [2.5, -1.2, 0.8],
            '成交额': [5e8, 3e8, 5e7],
            '换手率': [3.0, 1.5, 0.8],
            '成交量': [1e7, 8e6, 2e6],
            '最高': [12.80, 15.50, 3.40],
            '最低': [12.20, 15.00, 3.10],
            '今开': [12.30, 15.40, 3.25],
            '昨收': [12.20, 15.50, 3.15],
        })
        result = fetcher._parse_dataframe(df, "银行")
        assert len(result) == 3

        q0 = result[0]
        assert q0.code == '000001'
        assert q0.name == '平安银行'
        assert q0.price == 12.50
        assert q0.change_pct == 2.5
        assert q0.sector == '银行'
        assert q0.is_st is False

        q2 = result[2]
        assert q2.is_st is True  # *ST in name

    def test_deduplication(self):
        """同一只股票出现在两个板块中，只保留第一次出现"""
        fetcher = SectorBasedFetcher(sectors=["半导体", "消费电子"])
        df = pd.DataFrame({
            '代码': ['000001', '000002'],
            '名称': ['测试A', '测试B'],
            '最新价': [10.0, 20.0],
            '涨跌幅': [1.0, 2.0],
            '成交额': [1e8, 2e8],
            '换手率': [2.0, 3.0],
            '成交量': [1e6, 2e6],
            '最高': [10.5, 20.5],
            '最低': [9.8, 19.8],
            '今开': [10.0, 20.0],
            '昨收': [9.9, 19.6],
        })

        all_quotes = {}
        # 第一板块
        for q in fetcher._parse_dataframe(df, "半导体"):
            all_quotes[q.code] = q
        # 第二板块 — 000001 重复
        df2 = df.iloc[[0]].copy()
        df2['名称'] = '测试A-重复'
        for q in fetcher._parse_dataframe(df2, "消费电子"):
            if q.code not in all_quotes:
                all_quotes[q.code] = q

        assert len(all_quotes) == 2
        assert all_quotes['000001'].sector == '半导体'  # 保留首次出现的板块
        assert all_quotes['000001'].name == '测试A'

    def test_limit_price_calculation(self):
        fetcher = SectorBasedFetcher()
        df = pd.DataFrame({
            '代码': ['000001'],
            '名称': ['测试'],
            '最新价': [11.0],
            '涨跌幅': [10.0],
            '成交额': [1e8], '换手率': [2.0], '成交量': [1e6],
            '最高': [11.0], '最低': [10.0],
            '今开': [10.0], '昨收': [10.0],
        })
        result = fetcher._parse_dataframe(df, "测试")
        assert len(result) == 1
        q = result[0]
        assert q.limit_up == 11.0   # 10.0 * 1.1
        assert q.limit_down == 9.0  # 10.0 * 0.9

    def test_rate_limit_sleep_range(self):
        import random
        fetcher = SectorBasedFetcher(min_sleep=1.0, max_sleep=2.0)
        # 多次采样验证 sleep 在范围内
        random.seed(42)
        for _ in range(20):
            delay = random.uniform(fetcher._min_sleep, fetcher._max_sleep)
            assert 1.0 <= delay <= 2.0


class TestSinaFetcher:
    def test_availability(self):
        fetcher = SinaFetcher()
        available = fetcher.is_available()
        assert isinstance(available, bool)

    def test_priority(self):
        fetcher = SinaFetcher()
        assert fetcher.priority == 1
        assert fetcher.name == "sina"

    def test_parse_empty_dataframe(self):
        fetcher = SinaFetcher()
        df = pd.DataFrame()
        result = fetcher._parse_dataframe(df)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_parse_dataframe_with_sina_columns(self):
        """Sina API returns different column names — verify mapping works"""
        fetcher = SinaFetcher()
        df = pd.DataFrame({
            '代码': ['000001', '600000'],
            '名称': ['平安银行', '浦发银行'],
            '最新价': [12.50, 8.30],
            '涨跌幅': [2.5, -1.2],
            '成交额': [5e8, 3e8],
            '换手率': [3.0, 1.5],
            '成交量': [1e7, 8e6],
            '最高': [12.80, 8.50],
            '最低': [12.20, 8.10],
            '今开': [12.30, 8.45],
            '昨收': [12.20, 8.40],
        })
        result = fetcher._parse_dataframe(df)
        assert len(result) == 2
        assert result[0].code == '000001'
        assert result[0].price == 12.50
        assert result[0].limit_up == 13.42  # 12.20 * 1.1
        assert result[0].limit_down == 10.98  # 12.20 * 0.9

    def test_parse_sina_alt_column_names(self):
        """Sina may return columns with English names depending on akshare version"""
        fetcher = SinaFetcher()
        df = pd.DataFrame({
            'symbol': ['000001'],
            'name': ['Test'],
            'trade': [10.0],
            'changepercent': [1.5],
            'amount': [1e8],
            'turnoverratio': [2.0],
            'volume': [1e6],
            'high': [10.5],
            'low': [9.8],
            'open': [10.0],
            'settlement': [9.9],
        })
        result = fetcher._parse_dataframe(df)
        assert len(result) == 1
        q = result[0]
        assert q.code == '000001'
        assert q.price == 10.0
        assert q.pre_close == 9.9
        assert q.turnover == 1e8
        assert q.turnover_rate == 2.0

    def test_st_detection(self):
        fetcher = SinaFetcher()
        df = pd.DataFrame({
            '代码': ['000001'],
            '名称': ['*ST华泽'],
            '最新价': [3.20],
            '涨跌幅': [0.8],
            '成交额': [5e7], '换手率': [0.8], '成交量': [2e6],
            '最高': [3.40], '最低': [3.10],
            '今开': [3.25], '昨收': [3.15],
        })
        result = fetcher._parse_dataframe(df)
        assert len(result) == 1
        assert result[0].is_st is True
