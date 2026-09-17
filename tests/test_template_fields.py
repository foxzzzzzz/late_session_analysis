"""模板字段回归测试

背景: report.j2 曾引用已废弃字段 afternoon_volume_ratio (现名 late_volume_ratio),
因只在 strong_buy 非空时才渲染, bug 潜伏数月才在实盘暴露。

本测试静态校验所有模板引用的 stock.* 字段都存在于 StockContext,
并做一次强买场景的端到端渲染, 防止同类回归。
"""
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from screening.context import StockContext


TEMPLATE_DIR = _PROJECT_ROOT / "report" / "templates"


def _template_stock_fields(tpl_path: Path) -> set[str]:
    """提取模板中引用的 stock.xxx 字段名"""
    import re
    text = tpl_path.read_text(encoding="utf-8")
    return set(re.findall(r"\bstock\.([a-zA-Z_][a-zA-Z0-9_]*)", text))


class TestTemplateFields:
    """模板字段静态校验"""

    def test_all_template_fields_exist(self):
        """所有模板引用的 stock.* 字段必须存在于 StockContext"""
        known = {f.name for f in dc_fields(StockContext)}
        templates = sorted(TEMPLATE_DIR.glob("*.j2"))
        assert templates, "未找到任何模板文件"

        problems = []
        for tpl in templates:
            for fname in _template_stock_fields(tpl):
                if fname not in known:
                    problems.append(f"{tpl.name}: stock.{fname}")

        assert not problems, (
            "模板引用了 StockContext 不存在的字段:\n  "
            + "\n  ".join(problems)
        )

    def test_no_legacy_field_names(self):
        """确保旧字段名不再出现 (已重命名的字段)"""
        legacy = {
            "afternoon_volume_ratio",  # → late_volume_ratio
        }
        for tpl in TEMPLATE_DIR.glob("*.j2"):
            used = _template_stock_fields(tpl)
            hit = used & legacy
            assert not hit, f"{tpl.name} 仍在使用废弃字段: {hit}"


class TestStrongBuyRendering:
    """强买场景端到端渲染 (历史 bug 触发路径)"""

    def _make_ctx(self):
        ctx = StockContext(code="000001", name="平安银行", price=12.5, change_pct=3.2)
        for k, v in dict(
            final_score=78.0, total_score=78.0, recommendation="strong_buy",
            late_price_change=1.5, late_volume_ratio=2.3, anomaly_type="rally",
            turnover_rate=3.5, big_order_net=5e7, big_order_ratio=0.05,
            active_buy_ratio=55.0, position_20d=60.0, sector="银行",
            sector_performance=1.2, ma_alignment="bullish",
            score_tail_strength=25, score_technical=20, score_capital=15,
            score_ma_system=12, score_market_env=6,
            llm_decision="buy", llm_confidence="A", llm_score=80.0, llm_reason="test",
        ).items():
            setattr(ctx, k, v)
        return ctx

    def _make_stats(self):
        return dict(
            initial=5000, s0=1841, s1_l1=1074, s1_kline=100, l2=20, l3=5, l4=3,
            s0_ratio=36.8, s1_ratio=5.4, l2_ratio=20.0, l3_ratio=25.0,
            elapsed=1300, llm_api_success=3, llm_total=3, llm_fallback_count=0,
            llm_buy_signals=1, llm_disabled=False, capital_data_available=True,
            capital_data_date="today", northbound_available=False,
            northbound_today_net=0, northbound_trend=50, northbound_label="N/A",
            concept_total=200, concept_top5=[], concept_occurrences=500,
            dq_daily_kline=3, dq_5min_kline=3, dq_fund_flow=3,
            dq_late_metrics=3, dq_ma_calculated=3,
        )

    def test_render_strong_buy_card(self):
        """强买卡片渲染不得抛异常, 且含关键指标行"""
        from report.renderer import render_report

        md = render_report(
            strong_buy=[self._make_ctx()],
            buy_stocks=[],
            watch_stocks=[],
            stats=self._make_stats(),
            market_overview={
                "indices": [{"name": "上证指数", "close": 3200.0,
                             "change_pct": 0.5, "change_amt": 16.0}],
                "breadth": {"up": 3000, "down": 1500, "flat": 100,
                            "ratio": 2.0, "label": "偏多"},
                "hot_sectors": [],
            },
        )
        assert "强烈买入" in md
        assert "尾盘量比" in md
        # 旧字段名不应出现在输出中
        assert "afternoon_volume_ratio" not in md
