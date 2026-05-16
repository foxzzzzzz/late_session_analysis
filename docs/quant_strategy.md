# 尾盘量化策略文档

> 版本: 1.0 | 日期: 2026-05-16 | 状态: S0-S4已实现, S5-S7待实现

---

## 1. 策略概述

### 1.1 交易逻辑

A股 T+0 尾盘买入 / T+1 次日开盘卖出 的短线交易策略。核心假设：尾盘（14:30-15:00）的资金异动和价格行为包含了对次日走势的预测信息。

### 1.2 时间线

```
14:25 ──── 14:30 ──────── 14:50 ────── 14:55 ─── 14:57 ── 14:58
  │          │               │            │        │        │
  S0         S1              S2           S3       S4       下单
 板块预筛选   K线+L1准入      尾盘异动      均线验证  融合评分   (14:58)
 1次         每3min循环      每1min循环    每30s    每10s
```

### 1.3 数据源策略

| 数据类型 | 数据源 | 频率 | 说明 |
|----------|--------|------|------|
| 实时量价 | Tencent `qt.gtimg.cn` | 5s-3min | PE/PB/市值/量比/振幅，免费不限流 |
| 板块排名 | 同花顺 `stock_board_industry_summary_ths` | S0一次 | 90个行业涨跌幅 |
| 板块成分股 | 东财 `stock_board_industry_cons_em` | S0一次 | 含重试(3次指数退避) |
| 个股资金流向 | 东财 `push2his` | **S2首轮仅一次** | ≤300只，避免封IP |
| 均线 | mootdx | S3每30s | TCP通达信 |
| 北向情绪 | 同花顺 `hsgtApi` | S3一次 | 自缓存降级 |
| 题材标签 | 同花顺热点归因 | 盘前一次 | 静态预加载 |

**核心原则**: 量价走Tencent高频免费通道；资金流向仅S2首轮拉取一次；S3用Tencent量价变化近似替代资金趋势。

---

## 2. 数据模型

### 2.1 StockContext（统一数据结构）

所有阶段共用同一对象，各层读写各自字段。

#### 基础标识
| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `code` | str | 数据源 | 股票代码 |
| `name` | str | 数据源 | 股票名称 |

#### L1 实时行情（来自Tencent）
| 字段 | 类型 | 说明 |
|------|------|------|
| `price` | float | 当前价格 |
| `change_pct` | float | 涨跌幅 % |
| `turnover` | float | 成交额（元） |
| `turnover_rate` | float | 换手率 % |
| `volume` | float | 成交量（股） |
| `high` / `low` / `open` / `pre_close` | float | 最高/最低/开盘/昨收 |
| `limit_up` / `limit_down` | float | 涨停价/跌停价 |
| `is_st` | bool | 是否ST |
| `is_suspended` | bool | 是否停牌 |
| `sector` | str | 所属板块（Tencent无此字段，S0后回填） |
| `market_cap` | float | 总市值（亿元） |
| `pe_ttm` | float | 市盈率 TTM |
| `pb` | float | 市净率 |
| `vol_ratio` | float | 量比（当日成交量/5日均量） |
| `amplitude` | float | 振幅 % |

#### L1 计算字段（流动性指标）
| 字段 | 类型 | 说明 |
|------|------|------|
| `afternoon_volume` | float | 午后成交量 |
| `morning_volume` | float | 上午成交量 |
| `last_5min_volume` | float | 最后5分钟成交量 |
| `avg_period_volume` | float | 全天时段均量 |
| `afternoon_volume_ratio` | float | 午后/上午量比 |
| `last_5min_volume_pct` | float | 最后5分钟量占全天 % |
| `l1_passed` | bool | L1是否通过 |

#### L2 异动检测
| 字段 | 类型 | 说明 |
|------|------|------|
| `late_price_change` | float | 14:30后价格变动 % |
| `price_at_1430` | float | 14:30时刻价格 |
| `intraday_high` | float | 日内最高价 |
| `broke_high` | bool | 是否突破日内高点 |
| `big_order_net` | float | 大单净流入（元） |
| `big_order_ratio` | float | 大单占比 |
| `daily_avg_big_order_ratio` | float | 日内平均大单占比 |
| `active_buy_ratio` | float | 主动买入占比 % |
| `bid_vol` / `ask_vol` | float | 买一/卖一挂单量 |
| `cancel_rate` | float | 撤单率 % |
| `anomaly_type` | str | 异动类型: `rally` / `steady` / `breakout` / `volume_only` |
| `l2_passed` | bool | L2是否通过 |

#### S1 K线形态
| 字段 | 类型 | 说明 |
|------|------|------|
| `kline_passed` | bool | K线形态是否通过 |

#### L3 技术面
| 字段 | 类型 | 说明 |
|------|------|------|
| `ma5` / `ma10` / `ma20` | float | 5/10/20日均线 |
| `ma_alignment` | str | 均线排列: `above_ma5` / `bullish` / `bottom_area` |
| `position_20d` | float | 20日价格分位数 % |
| `near_key_level` | bool | 是否接近关键价位 |
| `sector_performance` | float | 所属板块涨跌幅 % |
| `sector_rank_pct` | float | 板块排名分位数 |
| `hot_concepts` | list[str] | 关联热点题材 |
| `leader_strength` | bool | 是否为板块龙头 |
| `volatility` | float | 年化波动率 % |
| `consecutive_limit_ups` | int | 连续涨停天数 |
| `has_bad_news` | bool | 是否有重大利空 |
| `is_unlock_date` | bool | 是否接近限售解禁日 |
| `history_win_rate` | float | 历史相似形态胜率 % |
| `l3_passed` | bool | L3是否通过 |

#### L4 评分
| 字段 | 类型 | 说明 |
|------|------|------|
| `score_tail_strength` | float | 尾盘强度分 (0-100) |
| `score_technical` | float | 技术面分 (0-100) |
| `score_capital` | float | 资金面分 (0-100) |
| `score_market_env` | float | 市场环境分 (0-100) |
| `score_history` | float | 历史胜率分 (0-100) |
| `total_score` | float | 加权总分 (0-100) |

#### 输出（LLM融合后）
| 字段 | 类型 | 说明 |
|------|------|------|
| `final_score` | float | LLM+规则融合分 (0-100) |
| `recommendation` | str | `strong_buy` / `buy` / `watch` / `skip` |

#### 计算属性
| 属性 | 逻辑 |
|------|------|
| `is_limit_up` | `abs(price - limit_up) < 0.005` |
| `is_limit_down` | `abs(price - limit_down) < 0.005` |
| `is_one_word_limit` | 涨停且 `abs(open - limit_up) < 0.005` 或跌停 `abs(open - limit_down) < 0.005` |

---

## 3. S0 — 板块预筛选

### 3.1 目标
从 ~5000只全市场 缩小到 200-500只候选池

### 3.2 参数

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `top_n` | 3 | `S0_TOP_N` | 初始入选板块数 |
| `max_n` | 5 | `S0_MAX_N` | 最大扩展板块数 |
| `min_stocks` | 200 | `S0_MIN_STOCKS` | 候选池最小股票数 |

### 3.3 流程

```
1. 获取板块排名
   ├── preloader.sector_performance (缓存) → 24个上涨板块
   ├── ak.stock_board_industry_summary_ths() (实时API)
   └── .env TARGET_SECTORS (静态降级，统一赋予涨幅1.0%)

2. 过滤：仅保留涨幅 > 0 的板块，按涨幅降序排列

3. 选取 Top N:
   ├── 初始取 Top 3
   ├── 成分股 < 200只 → 扩展到 Top 4
   └── 仍不足 → 扩展到 Top 5

4. 获取成分股 (ak.stock_board_industry_cons_em)
   ├── 对每个入选板块获取成分股列表
   ├── 合并去重，建立 stock→sector 映射
   └── 失败重试: 3次，指数退避(1s→2s→4s)+随机抖动

5. 输出: (候选代码列表, 代码→板块名称字典)
```

### 3.4 依赖数据
- 同花顺90个行业当日涨跌幅
- 东财行业成分股列表

---

## 4. S1 — 基础准入 + K线形态

### 4.1 L1 基础准入（7项，AND逻辑）

| # | 条件 | 参数 | 默认值 | 环境变量 |
|---|------|------|--------|----------|
| 1 | 非ST | `exclude_st` | True | — |
| 2 | 非停牌 | `exclude_suspended` | True | — |
| 3 | 非一字板 | `exclude_one_word_limit` | True | — |
| 4 | 成交额 ≥ 阈值 | `min_turnover` | 50,000,000 (5000万) | `L1_MIN_TURNOVER` |
| 5 | 换手率 ≥ 阈值 | `min_turnover_rate` | 1.0% | `L1_MIN_TURNOVER_RATE` |
| 6 | 价格在区间内 | `min_price` / `max_price` | 5.0 / 100.0 | `L1_MIN_PRICE` / `L1_MAX_PRICE` |
| 7 | 午后量 > 时段均量 | `afternoon_volume > avg_period_volume` | — | — |

**全部通过 → `l1_passed = True`**

### 4.2 K线形态筛选（3项，AND逻辑）

| # | 条件 | 参数 | 默认值 | 环境变量 |
|---|------|------|--------|----------|
| 1 | 振幅(ATR/Close) ≥ 阈值 | `min_atr_pct` | 1.5% | `KLINE_MIN_ATR_PCT` |
| 2 | 连涨天数 ≤ 阈值 | `max_consecutive_up` | 5天 | `KLINE_MAX_CONSECUTIVE_UP` |
| 3 | 阳线实体 ≥ 阈值 | `min_yang_body_pct` | 1.0% | `KLINE_MIN_YANG_BODY_PCT` |

**K线补充规则**:
- `change_pct > 9.5%`（接近涨停）→ 不通过（追高风险）
- `open < pre_close AND change_pct > 0`（低开高走）→ 通过（反转信号）
- `change_pct <= 0`（收阴）→ 不通过

**全部通过 → `kline_passed = True`**

### 4.3 依赖数据
- Tencent实时行情（价格/成交额/换手率/ST标记/停牌标记/量比/振幅）
- K线数据（preloader预加载或近似）

---

## 5. S2 — 尾盘异动检测

### 5.1 目标
识别尾盘30分钟内出现异常量价行为的股票

### 5.2 参数

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `volume_ratio_min` | 2.5 | `L2_VOLUME_RATIO` | 午后/上午量比阈值 |
| `last_5min_vol_pct_min` | 12.0% | `L2_LAST5MIN_VOLUME_PCT` | 最后5分钟量占比阈值 |
| `late_rally_min` | 3.0% | `L2_LATE_RALLY_PCT` | 尾盘拉升最小涨幅 |
| `recovery_drop_min` | 3.0% | `L2_LATE_RECOVERY_DROP` | 企稳形态：14:30前最小跌幅 |
| `recovery_rise_min` | 1.5% | `L2_LATE_RECOVERY_RISE` | 企稳形态：最小回升幅度 |
| `active_buy_ratio_min` | 55.0% | `L2_ACTIVE_BUY_PCT` | 主动买入占比阈值 |
| `big_order_net_min` | 0 | — | 大单净流入底线 |
| `big_order_ratio_mult` | 1.3 | — | 尾盘大单比 vs 日均倍数 |
| `cancel_rate_max` | 30.0% | — | 最大撤单率 |

### 5.3 通过逻辑

```
通过 = 放量条件满足 AND (价格形态满足 OR 资金流满足 OR 盘口满足)
```

#### 5.3.1 放量条件（至少满足1项）

| 条件 | 公式 |
|------|------|
| 午后放量 | `afternoon_volume_ratio >= 2.5` |
| 尾盘集中放量 | `last_5min_volume_pct >= 12.0%` |

#### 5.3.2 价格形态（按优先级匹配，取最高分）

| 类型 | 条件 | 分值 | 说明 |
|------|------|------|------|
| **rally** (尾盘拉升) | `late_price_change >= 3.0%` | = 尾盘涨幅 | 14:30后持续上涨，资金抢筹 |
| **breakout** (突破) | `broke_high == True` | 1.0 | 突破日内高点 |
| **steady** (企稳回升) | 14:30前跌 ≥ 3.0% AND 回升 ≥ 1.5% | = 回升幅度 | 早盘下跌后尾盘企稳，有资金托底 |

其中:
- `late_price_change` = 14:30后价格变动 %
- `broke_high` = `price >= intraday_high * 0.99`
- 企稳中 `drop_before_1430` = `(price_at_1430 - open) / open * 100`
- 企稳中 `recovery` = `late_price_change - drop_before_1430`

#### 5.3.3 资金流条件（可选，全部满足）
- `big_order_net >= 0`
- `big_order_ratio >= daily_avg_big_order_ratio * 1.3`（如果日均数据可用）
- `active_buy_ratio >= 55.0%`

#### 5.3.4 盘口条件（可选，全部满足）
- `bid_vol > ask_vol`
- `cancel_rate <= 30.0%`

### 5.4 资金流向富化（仅S2首轮执行一次）

```
1. 取 l1_passed 股票
2. 按 abs(change_pct) * vol_ratio * turnover 降序排列
3. 取前 max_capital_enrich=300 只
4. 调东财 push2his API 获取个股资金流向
5. 写入 ctx.big_order_net / ctx.big_order_ratio / ctx.active_buy_ratio
6. 设置 _fund_flow_fetched = True，后续轮次不再请求
```

### 5.5 依赖数据
- **实时分时数据**: 午后量比、尾盘量占比、14:30价格、日内最高、尾盘涨幅
- **资金流向**: 大单净流入、大单占比、主动买入比（仅S2首轮，东财push2his）
- **盘口数据**: 买卖挂单量、撤单率（当前MVP禁用）

---

## 6. S3 — 技术面验证

### 6.1 目标
过滤技术面不合格的股票，验证均线支撑和量价配合

### 6.2 参数

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `require_above_ma` | False | — | MVP: 不强制要求站上均线 |
| `position_20d_bottom_pct` | 50.0% | — | 20日底部区域阈值 |
| `sector_rank_top_pct` | 30.0% | `L3_SECTOR_RANK_TOP` | 板块排名阈值 |
| `min_history_win_rate` | 60.0% | `L3_HISTORY_WIN_RATE` | 最小历史胜率 |
| `max_volatility` | 50.0% | — | 最大年化波动率 |
| `max_consecutive_limits` | 1 | — | 最大连续涨停天数 |

### 6.3 通过条件

#### 硬排除（任一项不满足 → 不通过）
| # | 条件 | 逻辑 |
|---|------|------|
| 1 | 历史胜率 | `history_win_rate > 0 AND history_win_rate < 60.0%` → 排除 |
| 2 | 波动率 | `volatility > 0 AND volatility > 50.0%` → 排除 |
| 3 | 连续涨停 | `consecutive_limit_ups > 1` → 排除 |
| 4 | 利空消息 | `has_bad_news == True` → 排除 |
| 5 | 限售解禁 | `is_unlock_date == True` → 排除 |

#### 板块强度（信息性，不排除，影响L4评分）
- 从preloader获取 `sector_performance`
- 板块下跌 → 降低L4市场环境分

#### 技术位置（软条件，MVP不强制）
| 位置 | 条件 | `ma_alignment` |
|------|------|----------------|
| 站上MA5 | `price > ma5 > 0` | `above_ma5` |
| 多头排列 | 站上MA5 AND `ma5 > ma10` | `bullish` |
| 底部区域 | `position_20d <= 50.0%` | `bottom_area` |
| 关键价位 | `near_key_level` | — |

### 6.4 S3 Tencent量价近似（替代资金流向二次请求）

在S3不再请求push2，用S2→S3期间的量价变化近似资金趋势：

```
if vol_ratio_delta > 0 AND price_delta_pct > 0 (放量上涨):
    # 未从S2获取到资金流数据的股票
    big_order_net ≈ turnover * 0.02         # 近似2%为大单
    active_buy_ratio ≈ min(55 + vol_ratio_delta*2, 70)  # 上限70%
```

其中:
- `vol_ratio_delta` = 当前量比 - S2基线量比
- `price_delta_pct` = (当前价 - S2基线价) / S2基线价 × 100

### 6.5 依赖数据
- MA5/MA10/MA20（mootdx或近似）
- 20日价格分位数
- 历史胜率数据
- 板块涨跌幅
- 解禁日历、利空标记
- 北向资金情绪

---

## 7. S4 — 量化评分

### 7.1 目标
将S3通过的股票按6个维度综合评分，输出Top 30

### 7.2 参数

| 参数 | 默认值 | 环境变量 | 说明 |
|------|--------|----------|------|
| `high_attention_threshold` | 75.0 | — | >此分=重点关注 |
| `medium_attention_threshold` | 60.0 | — | 60-75=次重点 |
| `max_high_attention` | 15 | — | 最大重点关注数 |
| `max_total_output` | 30 | — | 最大输出数 |

### 7.3 维度权重

#### 有资金流数据
| 维度 | 权重 | 代码变量 |
|------|------|----------|
| 尾盘强度 | 30% | `score_tail_strength` |
| 技术面 | 20% | `score_technical` |
| 资金面 | 15% | `score_capital` |
| 市场环境 | 15% | `score_market_env` |
| 基本面 | 15% | `score_fundamental` |
| 历史胜率 | 5% | `score_history` |

#### 无资金流数据（权重重分配）
| 维度 | 权重 |
|------|------|
| 尾盘强度 | 38% (+8) |
| 技术面 | 27% (+7) |
| 资金面 | 0% |
| 市场环境 | 15% |
| 基本面 | 15% |
| 历史胜率 | 5% |

资金流可用性检测: `any(ctx.big_order_net != 0 OR ctx.big_order_ratio > 0 for ctx in contexts)`

### 7.4 总分公式

```
total_score = Σ (sub_score_i × weight_i)
```

每个子维度先归一化到0-100，再加权。

### 7.5 子维度评分细则

#### 7.5.1 尾盘强度（原始满分35，归一化: raw×100/35）

**尾盘涨幅（满分10）**
| 涨幅 | 得分 |
|------|------|
| >= 4.0% | 10 |
| >= 2.0% | 8 |
| >= 1.0% | 5 |
| > 0% | 2 |
| <= 0% | 0 |

**量能倍数（满分10，上限10）**
| 量比 | 基础分 |
|------|--------|
| >= 3.0 | 10 |
| >= 2.0 | 8 |
| >= 1.5 | 6 |
| >= 1.0 | 3 |
| < 1.0 | 0 |

附加:
- `last_5min_volume_pct >= 15%` → +2
- `last_5min_volume_pct >= 10%` → +1

**大单占比（满分10）**
| 大单比 | 得分 |
|--------|------|
| >= 0.30 | 10 |
| >= 0.20 | 8 |
| >= 0.10 | 5 |
| `big_order_net > 0` | 3 |
| <= 0 | 0 |

**盘口强度（满分5，仅当 bid_vol>0 AND ask_vol>0）**
| 买/卖比 | 得分 |
|---------|------|
| >= 2.0 | 5 |
| >= 1.5 | 3 |
| >= 1.0 | 1 |

#### 7.5.2 技术面（原始满分25，归一化: raw×100/25）

**突破形态（满分10）**
| 异动类型 | 得分 |
|----------|------|
| `breakout` | 10 |
| `rally` | 7 |
| `steady` | 5 |
| 其他 | 0 |

**均线支撑（满分8）**
| 排列 | 得分 |
|------|------|
| `bullish` | 8 |
| `above_ma5` | 5 |
| `bottom_area` | 4 |
| 无 | 0 |

**量价配合（满分7）**
| 条件 | 得分 |
|------|------|
| 价涨 AND 量比>1.5 | 7 |
| 价涨 AND 量比>1.0 | 4 |
| 价涨 | 2 |
| 不涨 | 0 |

#### 7.5.3 资金面（原始满分20，归一化: raw×100/20）

**大单净流入（满分10）**
| 净流入 | 得分 |
|--------|------|
| > 1000万 | 10 |
| > 500万 | 7 |
| > 0 | 4 |
| <= 0 | 0 |

**机构活跃度（满分10，上限10）**
| 条件 | 加分 |
|------|------|
| 大单比 >= 0.20 | +5 |
| 主动买入 >= 60% | +5 |
| 主动买入 >= 55% | +3 |

#### 7.5.4 市场环境（动态满分18或23，归一化: raw×100/max）

**板块强度（满分8）**
| 板块涨幅 | 得分 |
|----------|------|
| >= 3.0% | 8 |
| >= 1.0% | 5 |
| >= 0% | 3 |
| >= -1.0% | 1 |
| < -1.0% | 0 |

**题材热度（满分7）**
- 有概念分析器: `concept_analyzer.get_concept_score(ctx.hot_concepts) * 0.7`
- 无分析器: `min(len(hot_concepts) * 3, 7)`

**龙头效应（3分）**: `leader_strength == True` → +3

**北向情绪（+0~5分，将最大可能分从18提升到23）**
| 趋势分 | 加分 |
|--------|------|
| >= 70 | +5 |
| >= 55 | +3 |
| >= 45 | +1 |
| < 45 | +0（不提升上限） |

#### 7.5.5 历史胜率（直接映射0-100）

| 胜率 | 得分 |
|------|------|
| >= 80% | 100 |
| >= 70% | 80 |
| >= 60% | 50 |
| >= 50% | 30 |
| < 50% | 10 |

#### 7.5.6 基本面（上限100）

**PE估值（满分40）**
| PE TTM | 得分 |
|--------|------|
| 0 < PE <= 15 | 40 |
| 15 < PE <= 25 | 35 |
| 25 < PE <= 40 | 25 |
| 40 < PE <= 80 | 15 |
| PE > 80 | 5 |
| PE <= 0（亏损） | 0 |

**PB估值（满分25）**
| PB | 得分 |
|----|------|
| 0 < PB <= 3 | 25 |
| 3 < PB <= 6 | 15 |
| PB > 6 | 5 |
| PB <= 0 | 0 |

**市值质量（满分15）**
| 市值（亿） | 得分 |
|-----------|------|
| >= 500 | 15 |
| >= 100 | 10 |
| > 0 | 5 |

**题材丰富度（满分20）**
`score += min(len(hot_concepts) * 5, 20)`

### 7.6 分数等级

| 总分 | 等级 | 说明 |
|------|------|------|
| > 85 | 强烈买入 | 多重信号共振 |
| 75-85 | 买入 | 主要信号确认 |
| 60-75 | 观察 | 部分信号，需人工判断 |
| < 60 | 放弃 | 信号不足 |

---

## 8. LLM融合（S4阶段）

### 8.1 融合公式

```
final_score = total_score × 0.7 + llm_score × 0.3
```

### 8.2 LLM评分映射

**决策映射**:
| LLM决策 | 分数 |
|---------|------|
| `buy` | 90 |
| `hold` | 50 |
| `skip` | 0 |

**置信度权重**:
| 置信度 | 权重 |
|--------|------|
| A | 1.0 |
| B | 0.7 |
| C | 0.4 |

`llm_score = decision_score × confidence_weight`

### 8.3 最终推荐阈值

| final_score | 推荐 |
|-------------|------|
| >= 75 | `strong_buy` |
| >= 60 | `buy` |
| >= 45 | `watch` |
| < 45 | `skip` |

### 8.4 LLM调用策略
- 仅S4首轮调用，后续轮次复用首轮结果
- 并行执行: ThreadPoolExecutor, 30并发, 15s超时/只
- 失败降级: 超时/解析失败 → 使用rule_scorer规则兜底

### 8.5 规则兜底评分（LLM不可用时）

| 信号类别 | 条件 | 分数 |
|----------|------|------|
| 尾盘涨幅 | >= 4% | 3 |
| 尾盘涨幅 | >= 2% | 2 |
| 尾盘涨幅 | >= 1% | 1 |
| 午后量比 | >= 2.5 | 3 |
| 午后量比 | >= 1.5 | 2 |
| 午后量比 | >= 1.0 | 1 |
| 大单 | 比例>=0.3 且 净额>0 | 3 |
| 大单 | 净额>0 | 1 |
| 技术面 | 多头排列 | 2 |
| 技术面 | 站上MA5/底部 | 1 |

| 总分 | 决策 | 置信度 |
|------|------|--------|
| >= 8 | `buy` | `A` |
| >= 5 | `buy` | `B` |
| >= 3 | `hold` | `B` |
| < 3 | `skip` | `C` |

---

## 9. 近似/降级策略汇总

当管线使用腾讯数据源（无分时/资金流/盘口数据）时，应用以下近似：

| 缺失字段 | 近似公式 | 触发条件 |
|----------|----------|----------|
| `afternoon_volume_ratio` | `vol_ratio × 0.7` | vol_ratio >= 3.0 |
| `late_price_change` | `abs(change_pct) × 0.35` | late_price_change == 0 且 change_pct != 0 |
| `broke_high` | `price >= high × 0.99` | 始终 |
| `last_5min_volume_pct` | `min(vol_ratio × 4, 15.0)` | vol_ratio >= 3.0 |
| `volatility` | `amplitude × 5` | amplitude > 0 |
| `ma5` | `pre_close × 0.98` | ma5 == 0 |
| `ma10` | `pre_close × 0.97` | ma10 == 0 |
| `big_order_net` (S3) | `turnover × 0.02` | S3放量上涨 且 big_order_net==0 |
| `active_buy_ratio` (S3) | `min(55 + vol_ratio_delta × 2, 70)` | S3放量上涨 且 active_buy_ratio==0 |
| `sector` | S0映射回填 | Tencent无板块字段 |
| `sector_performance` | preloader查询 | 有板块名时 |

---

## 10. 配置参数速查表

### 10.1 S0 板块预筛选
| 变量 | 默认 | 说明 |
|------|------|------|
| `S0_TOP_N` | 3 | 初始入选板块数 |
| `S0_MAX_N` | 5 | 最大扩展板块数 |
| `S0_MIN_STOCKS` | 200 | 候选池下限 |

### 10.2 S1 准入 + K线
| 变量 | 默认 | 说明 |
|------|------|------|
| `L1_MIN_TURNOVER` | 50000000 | 最小成交额(元) |
| `L1_MIN_TURNOVER_RATE` | 1.0 | 最小换手率(%) |
| `L1_MIN_PRICE` | 5.0 | 最低价格 |
| `L1_MAX_PRICE` | 100.0 | 最高价格 |
| `KLINE_MIN_ATR_PCT` | 1.5 | 最小波动率(%) |
| `KLINE_MAX_CONSECUTIVE_UP` | 5 | 最大连涨天数 |
| `KLINE_MIN_YANG_BODY_PCT` | 1.0 | 最小阳线实体(%) |

### 10.3 S2 尾盘异动
| 变量 | 默认 | 说明 |
|------|------|------|
| `L2_VOLUME_RATIO` | 2.5 | 午后/上午量比阈值 |
| `L2_LAST5MIN_VOLUME_PCT` | 12.0 | 最后5分钟量占比(%) |
| `L2_LATE_RALLY_PCT` | 3.0 | 尾盘拉升阈值(%) |
| `L2_LATE_RECOVERY_DROP` | 3.0 | 企稳前跌幅(%) |
| `L2_LATE_RECOVERY_RISE` | 1.5 | 企稳回升幅度(%) |
| `L2_ACTIVE_BUY_PCT` | 55.0 | 主动买入占比(%) |

### 10.4 S3 技术验证
| 变量 | 默认 | 说明 |
|------|------|------|
| `L3_SECTOR_RANK_TOP` | 30.0 | 板块排名分位(%) |
| `L3_HISTORY_WIN_RATE` | 60.0 | 最小历史胜率(%) |

### 10.5 时间循环
| 变量 | 默认 | 说明 |
|------|------|------|
| `S1_LOOP_INTERVAL` | 180 | S1循环间隔(秒) |
| `S2_LOOP_INTERVAL` | 60 | S2循环间隔(秒) |
| `S3_LOOP_INTERVAL` | 30 | S3循环间隔(秒) |
| `S4_LOOP_INTERVAL` | 10 | S4循环间隔秒 |
| `MAX_CAPITAL_ENRICH` | 300 | 资金流最大请求数 |

### 10.6 LLM
| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_PROVIDER` | deepseek | LLM提供商 |
| `LLM_MODEL` | deepseek-v4-pro | 模型名称 |
| `LLM_MAX_TOKENS` | 512 | 最大Token数 |
| `LLM_TEMPERATURE` | 0.3 | 温度参数 |

---

## 11. 关键数据依赖层级

```
Layer 0: 日线OHLCV (总是可用)
  ├── price, change_pct, turnover, turnover_rate
  ├── high, low, open, pre_close
  ├── market_cap, pe_ttm, pb, amplitude, vol_ratio
  └── limit_up, limit_down, is_st, is_suspended

Layer 1: 静态预加载 (盘前可用)
  ├── sector_performance (90行业涨跌幅)
  ├── unlock_stocks (限售解禁日历)
  ├── hot_concepts (热点题材标签)
  └── K线均线 (MA5/MA10/MA20)

Layer 2: 分时数据 (盘中实时，回测不可用)
  ├── afternoon_volume, morning_volume
  ├── afternoon_volume_ratio
  ├── late_price_change, price_at_1430
  ├── broke_high, intraday_high
  └── last_5min_volume, last_5min_volume_pct

Layer 3: 资金流数据 (盘中实时，回测不可用)
  ├── big_order_net, big_order_ratio
  ├── active_buy_ratio
  └── daily_avg_big_order_ratio

Layer 4: 盘口数据 (盘中实时，回测不可用)
  ├── bid_vol, ask_vol
  └── cancel_rate
```
