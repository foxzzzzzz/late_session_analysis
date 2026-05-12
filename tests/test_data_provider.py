"""数据采集层测试"""
import pytest
from data_provider.base import RealtimeQuote
from data_provider.efinance_fetcher import EfinanceFetcher
from data_provider.akshare_fetcher import AkshareFetcher


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
