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

# 9. 资金流向数据源独立测试 (开盘后任何时间可测)
python tools/test_fund_flow.py                     # 默认5只
python tools/test_fund_flow.py --codes 600519,000858  # 指定股票
python tools/test_fund_flow.py --no-batch              # 仅健康检查

# 10. 资金流向三源交叉验证 (分钟线 vs 新浪 vs 东财)
python tools/verify_fund_flow.py                        # 默认3只
python tools/verify_fund_flow.py --codes 600519,000858  # 指定股票
```

## 诊断工具

### 资金流向测试 (`tools/test_fund_flow.py` & `tools/verify_fund_flow.py`)

- `test_fund_flow.py` — 测试东财 push2his 资金流向API是否可达，**无需等到14:25，开盘后任何时间可运行**
- `verify_fund_flow.py` — 三源交叉验证：分钟线(push2 fflow/kline) vs 新浪(MoneyFlow) vs 东财(push2his)，对比主力净额和active_buy_ratio一致性

```bash
# 完整测试 (健康检查 + 单只详细 + 批量)
python tools/test_fund_flow.py

# 三源交叉验证
python tools/verify_fund_flow.py

# 仅健康检查 (快速验证API可达性)
python tools/test_fund_flow.py --no-batch
```

**输出解读**：
- `分钟线` — push2 fflow/kline klt=1，盘中唯一实时分钟级资金流(主力/超大单/大单/中单/散户)，但无 active_buy_ratio
- `sina` — 新浪当日实时资金流向，提供正确的 active_buy_ratio（r0_in/(r0_in+r0_out)*100，0-100%范围）
- `push2his` — 东财日线资金流，仅收盘后返回当日数据，盘中返昨日
- `mainForce` — 主力净流入(万元)，正值=流入
- `active_buy_ratio` — 主动买入占比(%)，取自新浪(已修正)，>55偏多

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
| S0 板块预筛选 | 14:25-14:30 | .env板块 → baostock CSRC匹配 + akshare补充 | 1次 | ✅ 已实现 |
| S1 K线+量价 | 14:30-14:50 | Tencent (fetch_codes) | 每3min | ✅ 已实现 |
| S2 尾盘异常 | 14:50-14:55 | Tencent + 分钟线+Sina双源资金流(仅首次) | 每1min | ✅ 已实现 |
| S3 均线验证 | 14:55-14:57 | Tencent + mootdx 均线 | 每30s | ✅ 已实现 |
| S4 融合评分 | 14:57-14:58 | 纯计算 + LLM(仅首次) | 每10s | ✅ 已实现 |
| S5 下单执行 | 14:58-14:59:30 | Tencent + 盘口 | 每5s | 📋 计划中 |
| S6 收盘确认 | 14:59:30-15:00 | Tencent | 每5s | 📋 计划中 |
| S7 报告生成 | 15:00-15:05 | 全流程累积数据 | 1次 | 📋 计划中 |

## 数据刷新策略

| 数据类型 | 数据源 | 最高频率 | 说明 |
|----------|--------|---------|------|
| 量价 | Tencent `qt.gtimg.cn` | 每5s | 50只/批，免费不限流，全阶段共用 |
| 板块排名 | .env TARGET_SECTORS 直接匹配 | S0一次 | 5个配置板块 → 关键词匹配CSRC行业分类 |
| 板块成分股 | baostock CSRC缓存 + akshare补充 | S0一次 | 主力从JSON缓存匹配，akshare成功后合并去重 |
| 个股资金流向 | 分钟线(push2 fflow/kline, 实时mainForce) + 新浪(active_buy_ratio) + push2his(降级) | **S2仅首次** | 双源并发，按字段合并，避免高频触发IP封禁 |
| 均线 | mootdx | 每30s | TCP通达信，S3阶段 |
| 北向情绪 | 同花顺 hsgtApi | S3一次 | 自缓存，降级到昨日数据 |

**核心原则：** 不引入付费API。量价走Tencent高频免费通道，资金流向仅在S2首轮并发拉取双源——分钟线(实时mainForce/super/large/retail) + 新浪(active_buy_ratio, 修正为 r0_in/(r0_in+r0_out)*100)。新浪失败时降级到东财push2his。S3用Tencent量价变化近似替代资金趋势。

## 架构

```
数据采集层 (多源降级链)
  ├── Tencent (qt.gtimg.cn) — PE/PB/市值/量比/振幅，首选
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
  ├── 读取 .env TARGET_SECTORS (5个板块: 半导体,电子元件,通信设备,汽车零部件,计算机设备)
  ├── baostock CSRC行业分类关键词匹配 → 成分股 (主力)
  ├── akshare 成分股API补充 → 合并去重 (成功则合并)
  └── 输出: ~900-1000 候选池
         ↓
S1 K线扫描 (14:30-14:50, 每3min循环)
  ├── L1 基础准入: ST/停牌/涨停/低价/低换手过滤
  ├── K线形态 R1 (7项, 不通过淘汰): ATR范围 / 连涨≤5 / 近9天涨≤6 / 近4天≥3阳 / 连续收盘涨≥0.5% / 单日涨幅<6.5% / 单日涨幅<2倍ATR
  ├── K线形态 R2 (6项, 仅标记): 涨幅不骤降 / 不连续递减 / 阳线实体不连续缩小 / 今日最高>前3天 / 收盘>前3天开盘 / 不长上影线
  └── 输出: 200-500 → 100-200
         ↓
S2 尾盘异常 (14:50-14:55, 每1min循环)
  ├── L2 异常检测: 量比/尾盘拉升/突破/尾盘量占比
  ├── 资金流向: 双源并发拉取——分钟线(实时mainForce, ≤300只) + 新浪(active_buy_ratio)，按字段合并，后续复用
  └── 输出: 100-200 → 50-80
         ↓
S3 均线验证 (14:55-14:57, 每30s循环)
  ├── L3 技术验证: MA多头排列(MA5≥MA10+Price>MA20>MA30>MA60) / 收盘站稳MA5分层(1.01/1.005/1.0) /
  │    最低价不破MA5 / MA5渐进加速 / 量比1.1~2.0 / 连续缩量>10%禁入 /
  │    历史胜率/波动率/板块强度/解禁 / 接近关键位(整数关口+MA20/MA60±2%)
  ├── 利空排查: 东方财富公告API逐只查询近3日公告标题, 关键词匹配(减持/违规/亏损等)
  ├── 龙头效应: 板块内市值/涨幅/成交额排名百分位 (top30%/20%/25%视为龙头)
  ├── 资金流数据来自S2首轮缓存 (不重复请求)
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

对齐 尾盘策略0514.txt，5维100分制，子分直接累加：

| 维度 | 有权重(有当日资金流) | 无权重(无当日资金流) | 子因子 |
|------|---------------------|---------------------|--------|
| A: 尾盘强度 | 30 | 37 | 尾盘涨幅(8) + 放量倍数(8) + 大单占比(8) + 封单强度(6) |
| B: K线形态 | 25 | 31 | 连续阳线质量(8) + 涨幅稳定性(7) + 实体放大(5) + 突破前高(5) |
| C: 资金面 | 20 | 0 | 主力净流入(10) + 北向/机构动向(10) |
| D: 均线系统 | 15 | 19 | 多头排列(8) + MA5加速(4) + 收盘站稳MA5(3) |
| E: 市场环境 | 10 | 13 | 板块强度(6) + 概念热度(4) |

> **关键规则**: 仅当日实盘资金流计入C维度(分钟线或新浪任一返回当日数据即有效)。无当日数据时C维度归零，等比放大A/B/D/E至满分100。昨日数据不参与权重分配。

**分数等级:** >=85 超强信号(strong_buy) | >=75 强信号(buy) | >=60 中等(watch) | <60 弱(skip)

## 配置 (`.env`)

### 数据源
| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DATA_PROVIDER_PRIORITY` | 数据源优先级 (逗号分隔) | `tencent,sina,efinance,akshare` |
| `TARGET_SECTORS` | S0 目标板块列表 (CSRC关键词匹配) | `半导体,电子元件,通信设备,汽车零部件,计算机设备` |

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
| `TARGET_SECTORS` | 目标板块 (逗号分隔) | `半导体,电子元件,通信设备,汽车零部件,计算机设备` |

> S0 已简化为直接使用 `.env` TARGET_SECTORS 匹配板块成分股，不再需要动态排名。`S0_TOP_N` / `S0_MAX_N` / `S0_MIN_STOCKS` 已废弃。

### S1 K线形态

> Round 1 共 7 项检查: ATR范围 / 连涨≤5 / 近9天涨≤6 / 近4天≥3阳 / 连续收盘涨≥0.5% / 单日涨幅<6.5% / 单日涨幅<2倍ATR

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `KLINE_MIN_ATR_PCT` | 最小波动率(ATR/Close)% | `2.0` |
| `KLINE_MAX_ATR_PCT` | 最大波动率(ATR/Close)% | `8.5` |
| `KLINE_MAX_CONSECUTIVE_UP` | 最大连涨天数 | `5` |
| `KLINE_MAX_UP_IN_9DAYS` | 近9天最多涨几天 | `6` |
| `KLINE_MAX_SINGLE_DAY_PCT` | 单日涨幅上限% | `6.5` |
| `KLINE_MIN_YANG_RATIO_4D` | 近4天阳线占比最低 | `0.75` (3/4) |
| `KLINE_MIN_CONSECUTIVE_CLOSE_RISE` | 至少连续N天收盘上涨 (滑动窗口扫描) | `3` |
| `KLINE_MIN_CLOSE_RISE_PCT` | 连续上涨每天最低涨幅% | `0.3` |
| `KLINE_MAX_ATR_MULTIPLE` | 单日涨幅 ≤ N倍ATR | `2.0` |

**Round 2 深度验证** (不通过仅标记，不淘汰):

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `KLINE_MAX_DROP_RATIO` | 涨幅骤降判定 (后一天/前一天 < N) | `0.5` |
| `KLINE_MAX_CONSECUTIVE_DECLINE` | 连续递减天数阈值 | `3` |
| `KLINE_MAX_CONSECUTIVE_BODY_SHRINK` | 阳线实体连续缩小天数阈值 | `3` |
| `KLINE_MAX_UPPER_SHADOW_RATIO` | 上影线占实体比上限 | `0.6` |

### 筛选阈值

> **L2 尾盘异动是关键调参入口** — 阈值过严会导致 S2 通过率极低（<2%），过松则噪声过多。
> 实盘管线在 14:50-14:55 每 1 分钟重复扫描，同一股票有多次通过机会，单次阈值可偏高；
> 回测/单次快照场景需适当调低。**如果发现实盘也频繁无信号，应优先降低 L2 阈值。**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L1_MIN_TURNOVER` | 最小成交额 | `50000000` (5000万) |
| `L1_MIN_TURNOVER_RATE` | 最小换手率% | `1.0` |
| `L1_MIN_PRICE` / `L1_MAX_PRICE` | 价格区间 | `5.0` / `100.0` |
| `L2_VOLUME_RATIO` | 尾盘量比 (14:30后/13:00-14:30) | `1.2` |
| `L2_LAST5MIN_VOLUME_PCT` | 最后5分钟量占比% | `5.0` |
| `L2_LATE_RALLY_PCT` | 尾盘拉升最低涨幅% | `2.0` |
| `L2_LATE_RECOVERY_DROP` | 企稳: 14:30前跌幅% | `3.0` |
| `L2_LATE_RECOVERY_RISE` | 企稳: 14:45后回升% | `1.5` |
| `L2_ACTIVE_BUY_PCT` | 主动买入占比% | `55.0` |
| `L2_REQUIRE_CAPITAL` | 资金流向作为硬门槛 (false=仅评分使用) | `true` |
| `L2_MIN_PASS` | L2 最低通过数，不足时自动放宽资金条件 | `10` |
| `L2_BIG_ORDER_NET_MIN` | 大单净流入下限 (万元) | `0` |
| `L2_BIG_ORDER_RATIO_MULT` | 尾盘大单占比 / 全天平均 | `1.3` |
| `L2_CANCEL_RATE_MAX` | 最大撤单率% | `30.0` |
| `L2_REQUIRE_ORDERBOOK` | 盘口数据硬门槛 (MVP阶段不启用) | `false` |

### S3 技术面验证

> S3 在 14:55-14:57 对 L2 通过的 50-80 只做均线系统验证，新增量比/缩量/MA加速检查。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L3_SECTOR_RANK_TOP` | 板块排名前N%视为强势 | `30` |
| `L3_HISTORY_WIN_RATE` | 近5日收阳率最低要求% | `40` |
| `L3_VOL_RATIO_MIN` | 量比下限 | `1.1` |
| `L3_VOL_RATIO_MAX` | 量比上限 | `2.0` |
| `L3_VOLATILITY_MAX` | 最大波动率 (小数) | `0.50` |
| `L3_MA5_CLOSE_RATIO_MIN` | 收盘/MA5 最低比率 (1.01/1.005/1.0分层) | `1.0` |
| `L3_MA5_LOW_RATIO_MIN` | 最低价/MA5 最低比率 (不破MA5) | `0.98` |

### L4 量化评分 — 5维阶梯参数

> 所有阶梯格式为 `[[threshold, score], ...]` JSON数组，从高到低匹配。可通过 `.env` 同名变量覆盖。

**评分阈值:**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_HIGH_THRESHOLD` | strong_buy 阈值 | `85.0` |
| `L4_BUY_THRESHOLD` | buy 阈值 | `75.0` |
| `L4_MEDIUM_THRESHOLD` | watch 下限 | `60.0` |
| `L4_MAX_HIGH_ATTENTION` | strong_buy 最大输出数 | `15` |
| `L4_MAX_TOTAL_OUTPUT` | 最大输出总数 | `30` |

**A维度 尾盘强度 (max 30，无资金流 max 37):**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_LATE_PRICE_TIERS` | 尾盘涨幅阶梯 `[[%,分],...]` | `[[4.0,8],[2.0,6],[1.0,4],[0.0,2]]` |
| `L4_VOL_RATIO_TIERS` | 尾盘放量倍数阶梯 | `[[3.0,8],[2.0,6],[1.5,4],[1.0,2]]` |
| `L4_LAST5MIN_BONUS_TIERS` | 最后5分钟量占比加成 | `[[15.0,1.0],[10.0,0.5]]` |
| `L4_BIG_ORDER_TIERS` | 大单占比阶梯 | `[[0.3,8],[0.2,6],[0.1,4]]` |
| `L4_BIG_ORDER_NET_SCORE` | 大单净流入>0基础分 | `2.0` |
| `L4_BID_ASK_TIERS` | 买卖挂单比阶梯 | `[[2.0,6],[1.5,4],[1.0,2]]` |

**B维度 K线形态 (max 25，无资金流 max 31):**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_YANG_DAYS_TIERS` | 近4天阳线天数阶梯 | `[[4,8],[3,6],[2,4],[1,2]]` |
| `L4_CLOSE_RISE_TIERS` | 连续收盘上涨天数阶梯 | `[[4,7],[3,5],[2,3],[1,1]]` |
| `L4_VOLATILITY_PENALTY_TIERS` | 波动率惩罚 `[[小数,扣分],...]` | `[[0.40,-2],[0.30,-1]]` |
| `L4_BODY_AMPLIFYING_SCORE` | 实体放大得分 | `5.0` |
| `L4_YANG_NO_AMPLIFY_SCORE` | 阳线多但未放大得分 | `2.0` |
| `L4_BROKE_HIGH_SCORE` | 突破前高得分 | `5.0` |
| `L4_BREAKOUT_SCORE` | 异动类型=breakout得分 | `3.0` |

**C维度 资金面 (max 20，无资金流 max 0):**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_FLOW_NET_TIERS` | 主力净流入阶梯 (万元) | `[[1000,10],[500,7],[100,4]]` |
| `L4_FLOW_NET_POSITIVE_SCORE` | 主力净流入>0基础分 | `2.0` |
| `L4_FLOW_RATIO_SCORE` | 大单占比≥0.2加分 | `5.0` |
| `L4_ACTIVE_BUY_TIERS` | 主动买入占比阶梯 | `[[60,5],[55,3]]` |
| `L4_NORTHBOUND_TIERS` | 北向情绪趋势分阶梯 | `[[70,3],[55,1]]` |

**D维度 均线系统 (max 15，无资金流 max 19):**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_MA_ALIGNMENT_SCORES` | 均线排列评分 `{"bullish":8,"above_ma5":5,...}` | 见.env.example |
| `L4_MA5_ACCEL_SCORE` | MA5加速得分 | `4.0` |
| `L4_PRICE_MA5_TIERS` | 收盘/MA5比率阶梯 | `[[1.01,3],[1.005,2],[1.0,1]]` |

**E维度 市场环境 (max 10，无资金流 max 13):**

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `L4_SECTOR_PERF_TIERS` | 板块涨跌幅阶梯 | `[[3,6],[1,4],[0,2],[-1,1]]` |
| `L4_CONCEPT_WEIGHT` | 概念分权重 (有analyzer) | `0.4` |
| `L4_CONCEPT_MAX` | 概念分上限 (有analyzer) | `4.0` |
| `L4_HOT_CONCEPT_PER_ITEM` | 每概念基础分 (无analyzer) | `1.5` |
| `L4_HOT_CONCEPT_MAX` | 概念分上限 (无analyzer) | `4.0` |
| `L4_LEADER_BONUS` | 龙头效应附加分 | `1.0` |

### Rule Scorer (LLM降级规则评分)

> 当LLM不可用或超时时的纯规则兜底方案。所有阶梯 `[[threshold, weight], ...]` 从高到低匹配。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RULE_LATE_PRICE_TIERS` | 尾盘涨幅信号阶梯 | `[[4,3],[2,2],[1,1]]` |
| `RULE_VOL_RATIO_TIERS` | 量比信号阶梯 | `[[2.5,3],[1.5,2],[1.0,1]]` |
| `RULE_BIG_ORDER_TIERS` | 大单信号阶梯 (ratio≥阈值且net>0) | `[[0.3,3]]` |
| `RULE_BIG_ORDER_NET_SCORE` | 大单净流入>0基础分 | `1.0` |
| `RULE_MA_BULLISH_SCORE` | 多头排列得分 | `2.0` |
| `RULE_MA_GOOD_SCORE` | 技术面良好得分 | `1.0` |
| `RULE_DECISION_TIERS` | 决策阶梯 `[[min_score,decision,confidence],...]` | `[[8,"buy","A"],[5,"buy","B"],[3,"hold","B"]]` |
| `RULE_DEFAULT_DECISION` | 默认决策 `[decision,confidence]` | `["skip","C"]` |

### Pipeline 时间窗口

> S2/S3/S4 循环截止时间，到点后自动退出循环。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `S2_WINDOW_END` | S2 尾盘异常扫描截止 | `14:55` |
| `S3_WINDOW_END` | S3 技术面验证截止 | `14:57` |
| `S4_WINDOW_END` | S4 融合评分截止 | `14:58` |

### 回测专用阈值

> 回测使用收盘快照做**单次判断**（非循环扫描），默认阈值比实盘宽松。
> 可通过 `BT_*` 环境变量独立调整，不影响实盘管线。

**L2 尾盘异动**

| 变量 | 说明 | 默认值 | 实盘默认值 |
|------|------|--------|-----------|
| `BT_L2_VOLUME_RATIO` | 下午/上午量比 | `1.5` | 1.5 |
| `BT_L2_LAST5MIN_VOL_PCT` | 最后5分钟量占比% | `6.0` | 8.0 |
| `BT_L2_LATE_RALLY_PCT` | 尾盘拉升% | `1.5` | 3.0 |
| `BT_L2_RECOVERY_DROP` | 企稳跌幅% | `2.0` | 3.0 |
| `BT_L2_RECOVERY_RISE` | 企稳回升% | `1.0` | 1.5 |
| `BT_L2_ACTIVE_BUY_PCT` | 主动买入占比% | `50.0` | 55.0 |
| `BT_L2_MIN_PASS` | L2 最低通过数，不足时放宽资金条件 | `10` | 10 |

> 回测硬编码 `require_capital=False`（资金流数据不可用），`BT_L2_MIN_PASS` 最低保障机制已内置但通常不触发。

**K线形态 (S1)**

> 回测默认比实盘宽松：阳线占比/收盘动量在实盘中通过多次循环扫描可捕获更多候选，
> 回测仅单次收盘快照，需调低。可通过 `BT_KLINE_*` 独立覆盖。

| 变量 | 说明 | 回测默认 | 实盘默认 |
|------|------|----------|----------|
| `BT_KLINE_MIN_ATR_PCT` | 最小波动率(ATR/Close)% | `2.0` | 2.0 |
| `BT_KLINE_MAX_ATR_PCT` | 最大波动率(ATR/Close)% | `8.5` | 8.5 |
| `BT_KLINE_MAX_CONSECUTIVE_UP` | 最大连涨天数 | `5` | 5 |
| `BT_KLINE_MAX_UP_IN_9DAYS` | 近9天最多涨几天 | `6` | 6 |
| `BT_KLINE_MAX_SINGLE_DAY_PCT` | 单日涨幅上限% | `6.5` | 6.5 |
| `BT_KLINE_MIN_YANG_RATIO_4D` | 近4天阳线占比最低 | `0.25` | 0.75 |
| `BT_KLINE_MIN_CONSECUTIVE_CLOSE_RISE` | 至少连续N天收盘上涨 | `0` (禁用) | 4 |
| `BT_KLINE_MIN_CLOSE_RISE_PCT` | 连续上涨每天最低涨幅% | `0.0` (禁用) | 0.5 |

**L4 推荐阈值**

> 回测无资金流/LLM，评分天然偏低，使用更低的推荐阈值作为补偿。

| 变量 | 说明 | 默认值 | 实盘默认值 |
|------|------|--------|-----------|
| `BT_L4_STRONG_BUY` | 强烈买入最低分 | `35.0` | 75.0 |
| `BT_L4_BUY` | 买入最低分 | `25.0` | 60.0 |
| `BT_L4_WATCH` | 观察最低分 | `15.0` | 45.0 |

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
# 全量测试 (121个)
pytest tests/ -v

# 单文件
pytest tests/test_layer1_access.py -v
```

## 回测系统 (Backtest)

基于历史数据 + 5分钟K线精确计算的策略回测验证系统。复用实盘管线 S1→S2→S3→S4 筛选逻辑，用固定板块股票池替代全市场扫描，T+1 开盘卖出模拟。

### 快速开始

```bash
# 默认日期区间 (.env 中 BT_START_DATE → BT_END_DATE)
python main_backtest.py

# 指定区间
python main_backtest.py --start 20260401 --end 20260515

# 自定义板块 + 强制刷新缓存
python main_backtest.py --sectors 半导体,电子元件 --no-cache

# 禁用5分钟线精确数据 (降级到日线近似公式)
python main_backtest.py --no-5min

# 详细日志
python main_backtest.py -v
```

### CLI 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--start` | 回测起始日期 YYYYMMDD | `.env` BT_START_DATE |
| `--end` | 回测结束日期 YYYYMMDD | `.env` BT_END_DATE |
| `--output-dir` | 报告输出目录 | `./backtest_reports` |
| `--cache-dir` | 数据缓存目录 | `./backtest_cache` |
| `--no-cache` | 禁用缓存，强制重新拉取 | false |
| `--no-5min` | 禁用5分钟线，使用近似公式 | false |
| `--capital-flow-mode` | 资金流模式: `none` / `estimated` | `none` |
| `--sectors` | 覆盖 TARGET_SECTORS (逗号分隔) | `.env` 配置 |
| `--max-positions` | 每日最大持仓数 | 5 |
| `--slippage` | 滑点 (bps) | 5.0 |
| `-v` | DEBUG 详细日志 | false |

### 回测报告

每次回测在 `--output-dir` 下生成时间戳命名的5个文件：

| 文件 | 内容 |
|------|------|
| `trades_*.csv` | 逐笔交易明细 (代码/名称/买入价/卖出价/收益率/评分等) |
| `summary_*.json` | 汇总统计 + 分层绩效 |
| `monthly_*.csv` | 月度收益分组统计 |
| `stratified_*.csv` | 按推荐等级 (strong_buy/buy/watch) 分层统计 |
| `overview_*.md` | Markdown 总览 (绩效表 + 分层表) |

### 与实盘管线的差异

| 维度 | 实盘管线 | 回测系统 |
|------|----------|----------|
| 股票池 | S0 .env TARGET_SECTORS 直接匹配 | 固定板块成分股 (~400-550只) |
| S1 K线形态 | mootdx TCP 日线 (8项R1+6项R2) | baostock 日线 (同参数，BT_KLINE_* 可独立调参) |
| S2 尾盘指标 | mootdx TCP 5分钟线 (实时) | baostock 5分钟线 (历史, 24/7可用, compute_s2_metrics) |
| S2 量价快照 | Tencent 实时快照 (14:50状态) | baostock 日线收盘快照 (全日终值) |
| 资金流向 | S2 首轮新浪MoneyFlow + 东财降级 | **不可用** (push2his/akshare/baostock 均无法获取历史资金流) |
| PE/PB/市值 | Tencent 实时 | **不可用** (baostock 日线不含估值字段) |
| 北向情绪 | 同花顺 hsgtApi 实时 | akshare 历史日度汇总 (中性分50, 无区分度) |
| 题材热度 | 同花顺热点归因 | **不可用** |
| 板块涨幅排名 | 同花顺实时 | **不可用** |
| LLM 融合 | 30% 权重 | **不可用** (rule_weight=1.0) |
| L4 评分 | 5维加权 (含资金面15%) | 4维加权 (资金面归零, 尾盘+8%/技术+7%) |
| 推荐阈值 | strong_buy≥75 | strong_buy≥35 (补偿评分偏低) |
| 买入价 | 实盘最新价 | 当日收盘价 + 滑点 |
| 卖出价 | 次日实盘开盘价 | 次日开盘价 - 滑点 |

### 可回测覆盖度

```
维度         策略权重(有资金流)  回测覆盖    说明
尾盘强度     30%               ~27%       4项核心S2指标精确，缺大单/盘口
技术面       20%               ~27%       异动类型100%精确
资金面       15%                 0%       push2his/akshare/baostock 均无法获取历史资金流
市场环境     15%               ~11%       板块/北向可用，题材无历史
历史胜率      5%                 5%       从日线数据计算
基本面       15%                 0%       PE/PB/市值不可用(baostock不含估值字段)
─────────────────────────────────────────
总有效覆盖:  ~70% (资金面+基本面归零时尾盘→38%/技术→27%)
```

核心差异化能力（S2尾盘异动检测）完整保留，回测对策略有效性有实质参考价值。

### 退出逻辑

```
买入价 = 当日收盘价 × (1 + 滑点/10000)
卖出价 = 次日开盘价 × (1 - 滑点/10000)
收益率 = (卖出 - 买入) / 买入 - 佣金 × 2
```

### 绩效指标

| 指标 | 说明 |
|------|------|
| 胜率 | 盈利交易占比 |
| 平均/中位数收益率 | 单笔收益分布 |
| 累计收益率 | 所有交易收益加总 |
| 夏普比率 | 年化 (sqrt(252))，基于单笔收益 |
| 最大回撤 | 累计净值曲线最大回撤 |
| Calmar 比率 | 年化收益 / 最大回撤 |
| 盈亏因子 | 总盈利 / 总亏损 |
| 最长连胜/连亏 | 连续盈利/亏损笔数 |

## 项目结构

```
late_session_analysis/
  main.py                         # CLI入口 (实盘)
  main_backtest.py                # CLI入口 (回测)
  data_provider/                  # 数据采集层
    ...
  screening/                      # 筛选漏斗 (实盘+回测共用)
    ...
  backtest/                       # 回测模块
    __init__.py
    config.py                     # BacktestConfig (继承 SystemConfig)
    data_loader.py                # 历史数据加载 + S2 精算
    data_adapter.py               # 5分钟线 → StockContext 适配
    engine.py                     # 逐日循环引擎 S1→S2→S3→S4
    trade_log.py                  # Trade, DayResult, 日志
    performance.py                # 绩效计算 (13项指标)
    report_generator.py           # CSV/JSON/Markdown 报告
  analysis/                       # LLM + 规则评分
    ...
  report/                         # 报告
    ...
  orchestration/                  # 编排
    ...
  tests/                          # 单元测试 (121个)
```

## 数据源说明

| 数据源 | 提供字段 | 依赖 | 用途 |
|--------|----------|------|------|
| 腾讯财经 | PE/PB/市值/量比/振幅/涨跌停价 | requests | S1-S4 主力量价源 |
| baostock CSRC | 5205只股票 → 83个CSRC行业分类 | 本地JSON缓存 | S0 板块成分股匹配 (主力) |
| akshare成分股 | 行业成分股列表 (东财API) | akshare | S0 候选池补充 |
| 东财分钟线(push2 fflow/kline) | 主力/超大单/大单/中单/散户实时净额 | urllib | S2 仅首轮 (主力, 盘中实时) |
| 新浪MoneyFlow | 主力/中单/小单/散单 + active_buy_ratio (r0_in/(r0_in+r0_out)*100) | requests | S2 仅首轮 (active_buy_ratio) |
| 东财push2his | 个股资金流向(主力/大单/active_buy_ratio) | requests | S2 降级通道 (收盘后有效) |
| 新浪财经 | 全市场快照 (24x7可用) | akshare | 降级数据源 |
| 同花顺热点 | 当日强势股 + 题材归因 | requests | S3 题材热度 |
| 同花顺北向 | 沪深股通分钟流向 | requests | S3 市场情绪 |
| 东财公告 | 个股公告标题+日期 (np-anotice-stock) | requests | S3 利空排查 (has_bad_news) |
| baostock CSRC缓存 | 83行业→成分股映射 | 本地JSON | S3 板块内排名 (leader_strength) |
| mootdx | K线 / 均线 / 财务快照 | mootdx | S3 MA计算 |
| **baostock** | **历史5分钟K线 (2015年至今)** | **baostock** | **回测 S2 精确计算 (24/7可用)** |

> **回测数据源**: 5分钟线使用 baostock (`query_history_k_data_plus`)，独立于东方财富，免费、无需 token，非交易时段可用。日线仍使用 akshare (24/7可用)。板块成分股优先使用 JSON 缓存 + akshare，降级到 baostock 名称匹配。

## 测试模式 vs 实盘模式

| 特性 | `--test` | 实盘 |
|------|----------|------|
| 时间限制 | 无 | 仅14:25-15:05 |
| S0成分股API | 可能不可用(东财盘后拒连) | 正常 |
| 资金流向API | 可能不可用(盘后东财拒连) | 分钟线+Sina双源，仅S2首轮 |
| 时间循环 | 每阶段仅1轮 | 按间隔循环 |
| 适用场景 | 开发调试、管线验证 | 实盘交易 |
