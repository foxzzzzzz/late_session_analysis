"""模板字段检查 — 验证 Jinja2 模板引用的 stock.* 字段都存在于 StockContext

背景: 报告模板曾引用已废弃字段 afternoon_volume_ratio (现名 late_volume_ratio),
因只在 strong_buy 非空时才渲染, bug 潜伏数月才暴露。

用法: python tools/check_template_fields.py
"""
import re
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

# 确保项目根目录在 sys.path (支持从任意位置运行)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

TEMPLATE_DIR = _PROJECT_ROOT / "report" / "templates"

# 模板中 stock.xxx 引用
FIELD_RE = re.compile(r"\bstock\.([a-zA-Z_][a-zA-Z0-9_]*)")

# Jinja2 内置/过滤器属性, 不算 StockContext 字段
IGNORED = {
    "items", "keys", "values", "get", "copy",
}


def get_context_fields() -> set[str]:
    from screening.context import StockContext
    return {f.name for f in dc_fields(StockContext)}


def main() -> int:
    try:
        known = get_context_fields()
    except ImportError as e:
        print(f"[ERROR] 无法导入 StockContext: {e}")
        return 2

    print(f"StockContext 字段数: {len(known)}")
    print(f"扫描模板目录: {TEMPLATE_DIR}\n")

    all_missing: dict[str, set[str]] = {}

    for tpl in sorted(TEMPLATE_DIR.glob("*.j2")):
        text = tpl.read_text(encoding="utf-8")
        used = set(FIELD_RE.findall(text))
        missing = {f for f in used if f not in known and f not in IGNORED}
        if missing:
            all_missing[tpl.name] = missing
            print(f"[!!] {tpl.name}: {len(used)} 个字段引用, 缺失 {len(missing)} 个")
            for f in sorted(missing):
                # 给出相近字段建议
                sugg = [k for k in known if f[:4] in k or k[:4] in f]
                hint = f"  (可能是 {sugg[:2]} ?)" if sugg else ""
                print(f"       - {f}{hint}")
        else:
            print(f"[OK] {tpl.name}: {len(used)} 个字段引用全部有效")

    print()
    if all_missing:
        total = sum(len(v) for v in all_missing.values())
        print("=" * 50)
        print(f"  发现 {total} 个无效字段引用, 需修复")
        print("=" * 50)
        return 1

    print("=" * 50)
    print("  所有模板字段引用有效")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
