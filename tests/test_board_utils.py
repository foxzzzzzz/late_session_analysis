"""board_utils 测试 — 涨跌停幅度计算"""
import pytest
from data_provider.board_utils import get_limit_pct, calc_limit_prices


class TestGetLimitPct:
    def test_main_board_shanghai(self):
        """上海主板 60xxxx → 10%"""
        assert get_limit_pct("600001") == 10.0
        assert get_limit_pct("601888") == 10.0
        assert get_limit_pct("603019") == 10.0

    def test_main_board_shenzhen(self):
        """深圳主板 00xxxx → 10%"""
        assert get_limit_pct("000001") == 10.0
        assert get_limit_pct("002230") == 10.0

    def test_gem_board(self):
        """创业板 30xxxx → 10%"""
        assert get_limit_pct("300750") == 10.0
        assert get_limit_pct("301234") == 10.0

    def test_star_market(self):
        """科创板 688xxx → 20%"""
        assert get_limit_pct("688001") == 20.0
        assert get_limit_pct("688981") == 20.0

    def test_beijing_exchange_4(self):
        """北交所 4xxxxx → 30%"""
        assert get_limit_pct("430001") == 30.0
        assert get_limit_pct("400123") == 30.0

    def test_beijing_exchange_8(self):
        """北交所 8xxxxx (非688) → 30%"""
        assert get_limit_pct("830001") == 30.0
        assert get_limit_pct("870123") == 30.0

    def test_st_stock(self):
        """ST股票 → 5%"""
        assert get_limit_pct("000001", is_st=True) == 5.0
        assert get_limit_pct("688001", is_st=True) == 5.0
        assert get_limit_pct("430001", is_st=True) == 5.0

    def test_short_code_padding(self):
        """短代码自动补齐到6位"""
        assert get_limit_pct("1") == 10.0
        assert get_limit_pct("000001") == 10.0

    def test_code_as_int(self):
        """整数代码也能正确处理"""
        assert get_limit_pct(688001) == 20.0
        assert get_limit_pct(1) == 10.0


class TestCalcLimitPrices:
    def test_main_board_prices(self):
        """主板 10% 涨停/跌停价"""
        lu, ld = calc_limit_prices(10.0, "000001")
        assert lu == 11.00
        assert ld == 9.00

    def test_star_market_prices(self):
        """科创板 20% 涨停/跌停价"""
        lu, ld = calc_limit_prices(10.0, "688001")
        assert lu == 12.00
        assert ld == 8.00

    def test_beijing_prices(self):
        """北交所 30% 涨停/跌停价"""
        lu, ld = calc_limit_prices(10.0, "430001")
        assert lu == 13.00
        assert ld == 7.00

    def test_st_prices(self):
        """ST 5% 涨停/跌停价"""
        lu, ld = calc_limit_prices(10.0, "000001", is_st=True)
        assert lu == 10.50
        assert ld == 9.50

    def test_with_explicit_limit_pct(self):
        """显式传入涨跌停幅度"""
        lu, ld = calc_limit_prices(10.0, limit_pct=15.0)
        assert lu == 11.50
        assert ld == 8.50

    def test_rounding(self):
        """四舍五入到2位小数"""
        lu, ld = calc_limit_prices(9.876, "000001")
        assert lu == round(9.876 * 1.1, 2)  # 10.86
        assert ld == round(9.876 * 0.9, 2)  # 8.89

    def test_no_code_no_pct(self):
        """未提供code和limit_pct → 默认10%"""
        lu, ld = calc_limit_prices(10.0)
        assert lu == 11.00
        assert ld == 9.00
