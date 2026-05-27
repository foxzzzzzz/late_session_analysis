"""报告渲染器 — Jinja2模板 + imgkit转图片 (参考DSA模式)"""
import os
import logging
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def get_jinja_env() -> Environment:
    return Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))


def render_report(
    strong_buy: list,
    buy_stocks: list,
    watch_stocks: list,
    stats: dict,
    data_source: str = "",
    market_overview: dict = None,
) -> str:
    """渲染主报告为Markdown"""
    env = get_jinja_env()
    template = env.get_template("report.j2")
    now = datetime.now()

    return template.render(
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
        data_source=data_source,
        strong_buy=strong_buy,
        buy_stocks=buy_stocks,
        watch_stocks=watch_stocks,
        stats=stats,
        market_overview=market_overview or {},
    )


def render_stock_card(ctx, recommendation: str = "") -> str:
    """渲染单只股票卡片"""
    env = get_jinja_env()
    template = env.get_template("stock_card.j2")
    return template.render(stock=ctx, recommendation=recommendation)


def save_report(markdown: str, output_dir: str = "./reports") -> str:
    """保存报告到文件"""
    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = f"tail_analysis_{now.strftime('%Y%m%d_%H%M%S')}.md"
    filepath = path / filename
    filepath.write_text(markdown, encoding="utf-8")
    logger.info(f"报告已保存: {filepath}")
    return str(filepath)


def md_to_image(markdown: str, output_dir: str = "./reports") -> str:
    """Markdown转图片 (需要wkhtmltoimage)"""
    import imgkit

    path = Path(output_dir)
    path.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    filename = f"tail_analysis_{now.strftime('%Y%m%d_%H%M%S')}.png"
    filepath = path / filename

    # 包装Markdown为HTML
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body {{ font-family: 'Microsoft YaHei', sans-serif; padding: 20px; max-width: 800px; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
th {{ background-color: #f5f5f5; }}
h1 {{ color: #333; }}
h2 {{ color: #666; border-bottom: 1px solid #eee; }}
</style></head><body>{markdown}</body></html>"""

    try:
        imgkit.from_string(html, str(filepath))
        logger.info(f"图片报告已保存: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.warning(f"Markdown转图片失败 (可能缺少wkhtmltoimage): {e}")
        return ""
