# A股尾盘分析系统 (Late Session Analysis)

基于实时行情扫描 + 4层漏斗筛选 + LLM辅助决策的A股尾盘交易分析系统。

## 核心定位

14:30-15:00 期间渐进式扫描全市场 5000+ 股票，识别尾盘存在交易潜力的标的，14:58 前给出买入建议，实现 T+0 尾盘买入、T+1 开盘卖出的短线交易策略。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 (可选，不配则纯规则评分)
cp .env.example .env
# 编辑 .env 填入 LLM_API_KEY

# 3. 快速测试 (任何时候可运行，纯规则评分)
python main.py --test --no-llm

# 4. 测试模式 + LLM分析 (需先配好 LLM_API_KEY)
python main.py --test

# 5. 实时模式 (仅交易日 14:25-15:05 有效)
python main.py

# 6. 定时调度 (14:29 自动启动)
python main.py --schedule

# 7. 仅数据拉取，不分析
python main.py --test --dry-run

# 8. 运行指定阶段
python main.py --test --stages 1,2      # 仅L1+L2
python main.py --test --stages 1,2,3    # L1+L2+L3
```

## 架构

```
数据采集层 (多源降级链)
  ├── Tencent (qt.gtimg.cn) — PE/PB/市值/量比/振幅，首选
  ├── Sina (SectorBased)    — 板块扫描，次选
  ├── Sina (全市场)          — 新浪全市场快照
  ├── Efinance              — 东方财富快照
  └── Akshare               — AKShare快照，兜底
         ↓
静态预加载 (14:30前完成)
  ├── 同花顺行业对比 — 90行业涨跌排名
  ├── 限售解禁日历   — 未来90天待解禁
  ├── 同花顺热点归因 — 当日强势股题材标签
  └── mootdx K线     — MA5/10/20 计算
         ↓
4层筛选漏斗 (14:30-14:58)
  S1 L1 基础准入: 5000 → ~3400 (ST/停牌/涨停/低价/低换手过滤)
  S2 L2 尾盘异动: ~3400 → ~370 (量比/尾盘拉升/突破)
  S3 L3 技术验证: ~370 → ~230 (均线/板块/解禁/历史胜率)
     百度资金富化: 主力净流入/大单占比/主动买入 (仅L3通过股)
     北向资金情绪: 同花顺hsgtApi + 本地自缓存
     题材热度分析: 同花顺热点词频统计
  S4 L4 量化评分: ~230 → Top30 (100分制多维评分)
         ↓
LLM/规则分析 (S4)
  ├── LLM并行分析 (LiteLLM, 30并发, 15s超时)
  └── 规则兜底 (LLM失败时自动降级，不阻塞)
         ↓
报告生成 (Jinja2 Markdown, 可选 imgkit 转PNG)
```

## L4 评分模型 (100分制)

| 维度 | 权重 | 评分因子 |
|------|------|----------|
| 尾盘强度 | 30% | 涨幅 + 放量 + 大单 + 封单 |
| 技术面 | 20% | 突破形态 + 均线排列 + 量价配合 |
| 资金面 | 15% | 主力净流入 + 机构动向 |
| 市场环境 | 15% | 板块强度 + 题材热度 + 北向情绪 |
| 基本面 | 15% | PE估值 + PB + 市值质量 + 热点题材 |
| 历史胜率 | 5% | 相似形态历史表现 |

**分数等级:** >75 重点关注 | 60-75 次重点 | <60 放弃

## 项目结构

```
late_session_analysis/
  main.py                         # CLI入口
  data_provider/                  # 数据采集层
    base.py                       # RealtimeQuote 数据类
    tencent_fetcher.py            # 腾讯财经 (PE/PB/市值)
    sina_fetcher.py               # 新浪全市场快照
    sector_fetcher.py             # 新浪板块扫描
    efinance_fetcher.py           # 东方财富快照
    akshare_fetcher.py            # AKShare快照
    baidu_flow_fetcher.py         # 百度资金流向
    northbound_fetcher.py         # 北向资金情绪 (同花顺hsgtApi)
    preloader.py                  # 静态数据预加载
    concept_analyzer.py           # 题材热度统计
    manager.py                    # 多源降级管理器
  screening/                      # 筛选漏斗
    context.py                    # StockContext 统一数据结构
    funnel.py                     # 漏斗流水线
    layer1_access.py              # L1 基础准入
    layer2_anomaly.py             # L2 尾盘异动
    layer3_technical.py           # L3 技术面验证
    layer4_scoring.py             # L4 量化评分
  analysis/                       # LLM + 规则评分
    llm_client.py                 # LiteLLM 客户端
    parallel_runner.py            # 并行LLM执行器
    merger.py                     # LLM/规则结果融合
    rule_scorer.py                # 规则兜底评分
  report/                         # 报告
    renderer.py                   # Jinja2 + imgkit 渲染
    templates/report.j2           # 主报告模板
    templates/stock_card.j2       # 个股卡片模板
  orchestration/                  # 编排
    config.py                     # 系统配置 (.env → dataclass)
    pipeline.py                   # 4阶段流水线
    stage_tracker.py              # 阶段计时
  tests/                          # 单元测试 (47个)
```

## 配置 (`.env`)

### 数据源
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATA_PROVIDER_PRIORITY` | 数据源优先级 (逗号分隔) | `tencent,sector_based,sina,efinance,akshare` |
| `TARGET_SECTORS` | 目标板块 | `半导体,软件开发,消费电子,通信设备,光伏设备,证券,汽车零部件,计算机设备` |

### LLM (可选，不配则纯规则评分)
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | LLM提供商 | `deepseek` |
| `LLM_MODEL` | 模型名称 | `deepseek-chat` |
| `LLM_API_KEY` | API密钥 | 空 |
| `LLM_API_BASE` | API地址 | `https://api.deepseek.com/v1` |

### 筛选阈值
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L1_MIN_TURNOVER` | 最小成交额 | `50000000` (5000万) |
| `L1_MIN_TURNOVER_RATE` | 最小换手率% | `1.0` |
| `L1_MIN_PRICE` / `L1_MAX_PRICE` | 价格区间 | `5.0` / `100.0` |
| `L2_VOLUME_RATIO` | 午后量比阈值 | `1.5` |
| `L2_LATE_RALLY_PCT` | 尾盘拉升阈值% | `2.0` |

### 其他
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENABLE_NORTHBOUND` | 北向资金情绪 | `true` |
| `REPORT_OUTPUT_DIR` | 报告输出目录 | `./reports` |

## 运行测试

```bash
pytest tests/ -v
```

## 数据源说明

| 数据源 | 提供字段 | 依赖 |
|--------|----------|------|
| 腾讯财经 | PE/PB/市值/量比/振幅/涨跌停价 | requests |
| 新浪财经 | 全市场快照 (24x7可用) | akshare |
| 百度股市通 | 主力/散户/大单资金流向 | requests |
| 同花顺热点 | 当日强势股 + 题材归因 | requests |
| 同花顺行业 | 90行业涨跌排名 | akshare |
| 同花顺北向 | 沪深股通分钟流向 | requests |
| mootdx | K线 / 财务快照 / F10 | mootdx |
