# A股尾盘分析系统 (Late Session Analysis)

基于实时行情扫描 + 7阶段时间循环漏斗 + LLM辅助决策的A股尾盘交易分析系统。

## 核心定位

14:25-15:05 期间渐进式扫描全市场 5000+ 股票，S0 板块预筛选缩小候选池，S1-S4 时间循环层层过滤，识别尾盘存在交易潜力的标的，14:58 前给出买入建议，实现 T+0 尾盘买入、T+1 开盘卖出的短线交易策略。

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

# 6. 定时调度 (14:25 自动启动)
python main.py --schedule

# 7. 仅数据拉取，不分析
python main.py --test --dry-run

# 8. 运行指定阶段 (0=S0, 1=S1, 2=S2, 3=S3, 4=S4)
python main.py --test --stages 0,1        # S0板块预筛选 + S1 K线扫描
python main.py --test --stages 0,1,2      # + S2尾盘异常
python main.py --test --stages 0,1,2,3,4  # 完整5阶段
```

## 7 阶段时间线

```
14:25 ──── 14:30 ──────── 14:50 ────── 14:55 ─── 14:57 ── 14:58 ── 14:59:30 ── 15:00 ── 15:05
  │          │               │            │        │        │           │         │
  S0         S1              S2           S3       S4       S5          S6        S7
 板块预筛选   K线+量价        尾盘异常      均线验证  融合评分  下单执行    收盘确认   报告生成
 1次         每3min循环      每1min循环    每30s    每10s    每5s        每5s      1次
 板块→200-   200-500只→      100-200只→   50-80只→ 30-50只→ (PLANNED)  (PLANNED) (PLANNED)
 500只       100-200只       50-80只      30-50只  Top 10
```

### 当前实现状态

| 阶段 | 时间窗口 | 数据源 | 频率 | 状态 |
|------|----------|--------|------|------|
| S0 板块预筛选 | 14:25-14:30 | 同花顺行业排名 + 东财成分股 | 1次 | ✅ 已实现 |
| S1 K线+量价 | 14:30-14:50 | Tencent (fetch_codes) | 每3min | ✅ 已实现 |
| S2 尾盘异常 | 14:50-14:55 | Tencent + 东财push2his(仅首次) | 每1min | ✅ 已实现 |
| S3 均线验证 | 14:55-14:57 | Tencent + mootdx 均线 | 每30s | ✅ 已实现 |
| S4 融合评分 | 14:57-14:58 | 纯计算 + LLM(仅首次) | 每10s | ✅ 已实现 |
| S5 下单执行 | 14:58-14:59:30 | Tencent + 盘口 | 每5s | 📋 计划中 |
| S6 收盘确认 | 14:59:30-15:00 | Tencent | 每5s | 📋 计划中 |
| S7 报告生成 | 15:00-15:05 | 全流程累积数据 | 1次 | 📋 计划中 |

## 数据刷新策略

| 数据类型 | 数据源 | 最高频率 | 说明 |
|----------|--------|---------|------|
| 量价 | Tencent `qt.gtimg.cn` | 每5s | 50只/批，免费不限流，全阶段共用 |
| 板块排名 | 同花顺 `stock_board_industry_summary_ths` | S0一次 | 已通过preloader缓存 |
| 板块成分股 | 东财 `stock_board_industry_cons_em` | S0一次 | 3-5次API调用，含重试+退避 |
| 个股资金流向 | 东财 push2his | **S2仅首次** | 避免高频触发IP封禁 |
| 均线 | mootdx | 每30s | TCP通达信，S3阶段 |
| 北向情绪 | 同花顺 hsgtApi | S3一次 | 自缓存，降级到昨日数据 |

**核心原则：** 不引入付费API。量价走Tencent高频免费通道，资金流向仅在S2首轮拉取一次，S3用Tencent量价变化近似替代资金趋势，避免触发东财IP封禁。

## 架构

```
数据采集层 (多源降级链)
  ├── Tencent (qt.gtimg.cn) — PE/PB/市值/量比/振幅，首选
  ├── Sina (SectorBased)    — 板块扫描，次选
  ├── Sina (全市场)          — 新浪全市场快照
  ├── Efinance              — 东方财富快照
  └── Akshare               — AKShare快照，兜底
         ↓
静态预加载 (14:25前完成)
  ├── 同花顺行业对比 — 90行业涨跌排名
  ├── 限售解禁日历   — 未来90天待解禁
  ├── 同花顺热点归因 — 当日强势股题材标签
  └── mootdx K线     — MA5/10/20 计算
         ↓
S0 板块预筛选 (14:25-14:30, 1次)
  ├── 同花顺行业涨幅排名 → Top 3-5 上涨板块
  ├── 东财成分股获取 → 合并去重 → stock→sector映射
  ├── 候选池不足200 → 自动扩展到Top 4 → Top 5
  └── 3级降级: preloader缓存 → 实时API → .env静态列表
         ↓
S1 K线扫描 (14:30-14:50, 每3min循环)
  ├── L1 基础准入: ST/停牌/涨停/低价/低换手过滤
  ├── K线形态: 波动率(ATR) / 连涨天数 / 阳线质量
  └── 输出: 200-500 → 100-200
         ↓
S2 尾盘异常 (14:50-14:55, 每1min循环)
  ├── L2 异常检测: 量比/尾盘拉升/突破/尾盘量占比
  ├── 资金流向: 首轮拉取东财push2his(≤300只)，后续复用
  └── 输出: 100-200 → 50-80
         ↓
S3 均线验证 (14:55-14:57, 每30s循环)
  ├── L3 技术验证: MA排列/历史胜率/波动率/板块强度/解禁
  ├── 资金趋势近似: Tencent量价变化替代push2 (不重复请求)
  └── 输出: 50-80 → 30-50
         ↓
S4 融合评分 (14:57-14:58, 每10s循环)
  ├── L4 量化评分: 100分制多维评分
  ├── LLM分析: 仅首轮调用(LiteLLM, 30并发, 15s超时)
  └── 输出: 30-50 → Top 30
         ↓
报告生成 (Jinja2 Markdown)
```

## 评分模型

| 维度 | 权重 | 评分因子 |
|------|------|----------|
| 尾盘强度 | 35% | 涨幅 + 量比 + 尾盘拉升 + 突破形态 |
| 技术面 | 25% | MA排列 + 均线支撑 + 量价配合 |
| 资金面 | 20% | 主力净流入(S2首轮) + 量价近似(S3) |
| 市场环境 | 15% | 板块强度 + 北向情绪 + 题材热度 |
| 历史胜率 | 5% | 相似形态历史表现 |

**分数等级:** >85 强烈买入 | 75-85 买入 | 60-75 观察 | <60 放弃

## 项目结构

```
late_session_analysis/
  main.py                         # CLI入口
  data_provider/                  # 数据采集层
    base.py                       # RealtimeQuote 数据类
    manager.py                    # 多源降级管理器
    tencent_fetcher.py            # 腾讯财经 (PE/PB/市值, fetch_codes)
    sina_fetcher.py               # 新浪全市场快照
    sector_fetcher.py             # 新浪板块扫描
    efinance_fetcher.py           # 东方财富快照
    akshare_fetcher.py            # AKShare快照
    baidu_flow_fetcher.py         # 百度资金流向
    northbound_fetcher.py         # 北向资金情绪 (同花顺hsgtApi)
    sector_filter.py              # S0板块预筛选 (同花顺排名→成分股)
    preloader.py                  # 静态数据预加载
    concept_analyzer.py           # 题材热度统计
  screening/                      # 筛选漏斗
    context.py                    # StockContext 统一数据结构
    funnel.py                     # 漏斗流水线 (支持K线层)
    layer1_access.py              # L1 基础准入
    layer2_anomaly.py             # L2 尾盘异动
    layer3_technical.py           # L3 技术面验证
    layer4_scoring.py             # L4 量化评分
    layer_kline.py                # S1 K线形态预筛选 (新增)
    cache.py                      # StockMetricsCache
  analysis/                       # LLM + 规则评分
    llm_client.py                 # LiteLLM 客户端
    parallel_runner.py            # 并行LLM执行器
    merger.py                     # LLM/规则结果融合
    rule_scorer.py                # 规则兜底评分
    prompts.py                    # LLM提示模板
  report/                         # 报告
    renderer.py                   # Jinja2渲染
    templates/report.j2           # 主报告模板
    templates/stock_card.j2       # 个股卡片模板
  orchestration/                  # 编排
    config.py                     # 系统配置 (.env → dataclass)
    pipeline.py                   # 7阶段时间循环流水线
    stage_tracker.py              # 阶段计时与状态追踪
  tests/                          # 单元测试 (47个)
```

## 配置 (`.env`)

### 数据源
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATA_PROVIDER_PRIORITY` | 数据源优先级 (逗号分隔) | `tencent,sector_based,sina,efinance,akshare` |
| `TARGET_SECTORS` | S0静态降级板块列表 | `半导体,软件开发,消费电子,通信设备,光伏设备,证券,汽车零部件,计算机设备` |

### LLM (可选，不配则纯规则评分)
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `LLM_PROVIDER` | LLM提供商 | `deepseek` |
| `LLM_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `LLM_API_KEY` | API密钥 | 空 |
| `LLM_API_BASE` | API地址 | `https://api.deepseek.com/v1` |

### S0 板块预筛选
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `S0_TOP_N` | 初始入选板块数 | `3` |
| `S0_MAX_N` | 最大扩展板块数 | `5` |
| `S0_MIN_STOCKS` | 候选池最小股票数 | `200` |

### S1 K线形态
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `KLINE_MIN_ATR_PCT` | 最小波动率(ATR/Close)% | `1.5` |
| `KLINE_MAX_CONSECUTIVE_UP` | 最大连涨天数 | `5` |
| `KLINE_MIN_YANG_BODY_PCT` | 最小阳线实体% | `1.0` |

### 筛选阈值
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L1_MIN_TURNOVER` | 最小成交额 | `50000000` (5000万) |
| `L1_MIN_TURNOVER_RATE` | 最小换手率% | `1.0` |
| `L1_MIN_PRICE` / `L1_MAX_PRICE` | 价格区间 | `5.0` / `100.0` |
| `L2_VOLUME_RATIO` | 量比阈值 | `2.5` |
| `L2_LATE_RALLY_PCT` | 尾盘拉升阈值% | `3.0` |
| `L2_ACTIVE_BUY_PCT` | 主动买入占比% | `55.0` |

### 时间循环间隔
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `S1_LOOP_INTERVAL` | S1 K线扫描间隔(秒) | `180` |
| `S2_LOOP_INTERVAL` | S2 尾盘异常间隔(秒) | `60` |
| `S3_LOOP_INTERVAL` | S3 均线验证间隔(秒) | `30` |
| `S4_LOOP_INTERVAL` | S4 评分冲刺间隔(秒) | `10` |

### 其他
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ENABLE_CAPITAL_FLOW` | S2资金流向富化 | `true` |
| `ENABLE_NORTHBOUND` | 北向资金情绪 | `true` |
| `MAX_CAPITAL_ENRICH` | 资金流向最大请求数 | `300` |
| `REPORT_OUTPUT_DIR` | 报告输出目录 | `./reports` |

## 运行测试

```bash
# 全量测试 (47个)
pytest tests/ -v

# 单文件
pytest tests/test_layer1_access.py -v
```

## 数据源说明

| 数据源 | 提供字段 | 依赖 | 用途 |
|--------|----------|------|------|
| 腾讯财经 | PE/PB/市值/量比/振幅/涨跌停价 | requests | S1-S4 主力量价源 |
| 同花顺行业 | 90行业涨跌排名 | akshare | S0 板块预筛选 |
| 东财成分股 | 行业成分股列表 | akshare | S0 候选池构建 |
| 东财push2his | 个股资金流向(主力/大单) | requests | S2 仅首轮 |
| 新浪财经 | 全市场快照 (24x7可用) | akshare | 降级数据源 |
| 百度股市通 | 主力/散户/大单资金流向 | requests | 降级资金流向 |
| 同花顺热点 | 当日强势股 + 题材归因 | requests | S3 题材热度 |
| 同花顺北向 | 沪深股通分钟流向 | requests | S3 市场情绪 |
| mootdx | K线 / 均线 / 财务快照 | mootdx | S3 MA计算 |

## 测试模式 vs 实盘模式

| 特性 | `--test` | 实盘 |
|------|----------|------|
| 时间限制 | 无 | 仅14:25-15:05 |
| S0成分股API | 可能不可用(东财盘后拒连) | 正常 |
| 资金流向API | 可能不可用 | 正常(仅S2首轮) |
| 时间循环 | 每阶段仅1轮 | 按间隔循环 |
| 适用场景 | 开发调试、管线验证 | 实盘交易 |
