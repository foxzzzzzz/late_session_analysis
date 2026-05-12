"""L1 基础准入筛选测试"""
import pytest
from screening.context import StockContext
from screening.layer1_access import screen_l1_access, L1Config


def make_ctx(code="000001", name="平安银行", **kwargs):
    """创建测试用StockContext"""
    defaults = {
        'code': code, 'name': name, 'price': 10.0, 'change_pct': 2.0,
        'turnover': 500_000_000, 'turnover_rate': 3.0, 'volume': 10_000_000,
        'high': 10.2, 'low': 9.8, 'open': 9.9, 'pre_close': 9.8,
        'limit_up': 10.78, 'limit_down': 8.82,
        'afternoon_volume': 6_000_000, 'avg_period_volume': 3_000_000,
    }
    defaults.update(kwargs)
    return StockContext(**defaults)


class TestLayer1Access:
    def test_normal_stock_passes(self):
        ctx = make_ctx()
        result = screen_l1_access([ctx])
        assert len(result) == 1
        assert ctx.l1_passed

    def test_st_stock_filtered(self):
        ctx = make_ctx(name="*ST凯乐", is_st=True)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_low_turnover_filtered(self):
        ctx = make_ctx(turnover=1_000_000)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_low_turnover_rate_filtered(self):
        ctx = make_ctx(turnover_rate=0.3)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_price_too_low_filtered(self):
        ctx = make_ctx(price=3.0)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_price_too_high_filtered(self):
        ctx = make_ctx(price=150.0)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_one_word_limit_up_filtered(self):
        ctx = make_ctx(open=10.78, price=10.78, limit_up=10.78)
        result = screen_l1_access([ctx])
        assert len(result) == 0

    def test_custom_config(self):
        config = L1Config(min_price=1.0, max_price=200.0)
        ctx = make_ctx(price=3.0)
        result = screen_l1_access([ctx], config)
        assert len(result) == 1

    def test_multiple_stocks(self):
        good = make_ctx(code="000001")
        st_stock = make_ctx(code="000002", name="*ST华信", is_st=True)
        low_turnover = make_ctx(code="000003", turnover=1_000_000)
        result = screen_l1_access([good, st_stock, low_turnover])
        assert len(result) == 1
        assert result[0].code == "000001"

    def test_batch_screening(self):
        """批量筛选1000只股票的性能"""
        stocks = [make_ctx(code=f"{i:06d}") for i in range(1000)]
        result = screen_l1_access(stocks)
        assert len(result) == 1000  # 全部符合默认条件
