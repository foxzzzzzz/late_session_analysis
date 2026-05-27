# 牛熊市双阈值策略设计

## 背景

当前所有筛选阈值全局统一，不区分市场环境。问题：

- **熊市/震荡市**：尾盘异动信号少，资金面偏弱。用牛市阈值会过度过滤，导致无信号。
- **牛市**：跟风资金多，信号多但质量参差。用熊市阈值会引入噪声。

## 市场状态判断

### 数据源（系统已有）

| 数据 | 来源 | 说明 |
|------|------|------|
| 北向资金趋势分 | 同花顺 hsgtApi `northbound_sentiment.trend_score` | 0-100, ≥70 bullish, ≤40 bearish |
| 上证指数方向 | mootdx 日线 `KlineProvider` | 可算 20日涨跌幅 |
| 板块轮动强度 | 同花顺行业对比 `preloader.sector_perf` | 90行业涨跌幅分布 |

### 判断逻辑

```python
def determine_regime(northbound, sh_index_20d_pct) -> str:
    bull_score = 0

    # 因子1: 北向情绪
    if northbound.get("trend_score", 50) >= 70:
        bull_score += 1
    elif northbound.get("trend_score", 50) <= 40:
        bull_score -= 1

    # 因子2: 大盘方向
    if sh_index_20d_pct >= 2:    # 上证20日涨超2% → 上升趋势
        bull_score += 1
    elif sh_index_20d_pct <= -2:  # 上证20日跌超2% → 下降趋势
        bull_score -= 1

    if bull_score > 0:
        return "bull"
    elif bull_score < 0:
        return "bear"
    else:
        return "neutral"   # 中性使用默认阈值
```

### 状态切换边界

- 中性状态使用默认阈值（与当前值一致）
- 连续 N 天同一状态才切换，避免单日噪声导致频繁切换
- 每日 14:25 管线启动时判定一次，当次运行使用同一套阈值

## 双阈值差异

### L2 尾盘异动

| 参数 | 牛市 (激进) | 中性 | 熊市 (保守) | 理由 |
|------|:---:|:---:|:---:|------|
| `active_buy_ratio_min` | 50 | 55 | 55 | 牛市跟风多，放低门槛；熊市只追强主力 |
| `volume_ratio_min` | 1.1 | 1.2 | 1.3 | 牛市放量普遍；熊市要显著放量确认 |
| `late_rally_min` | 1.5 | 2.0 | 2.5 | 牛市小拉升可追；熊市要大拉升 |
| `last_5min_vol_pct_min` | 4.0 | 5.0 | 6.0 | 同上逻辑 |

### L3 技术面

| 参数 | 牛市 | 中性 | 熊市 | 理由 |
|------|:---:|:---:|:---:|------|
| `max_volatility` | 0.65 | 0.60 | 0.55 | 牛市接受更高波动；熊市过滤波动股 |
| `vol_ratio_min` | 1.0 | 1.1 | 1.2 | 牛市不强制放量；熊市要求放量确认 |
| `min_history_win_rate` | 35 | 40 | 45 | 熊市要求更高的历史胜率 |

### L4 推荐阈值

| 参数 | 牛市 | 中性 | 熊市 | 理由 |
|------|:---:|:---:|:---:|------|
| `strong_buy` | 75 | 80 | 85 | 熊市评分要求更高（信号少但质量高） |
| `buy` | 65 | 72 | 75 | 同上 |
| `watch` | 50 | 55 | 60 | 同上 |

### K线形态

| 参数 | 牛市 | 中性 | 熊市 | 理由 |
|------|:---:|:---:|:---:|------|
| `min_yang_ratio_4d` | 0.60 | 0.75 | 0.75 | 牛市放低阳线要求 |
| `min_consecutive_close_rise` | 2 | 3 | 4 | 熊市需要更长的上涨惯性 |
| `max_volatility` (ATR) | 9.0 | 8.5 | 7.0 | 牛市波动率容忍度更高 |

## 实施计划

### Phase 1: 基础架构

1. 新建 `screening/market_regime.py` — `MarketRegime` 类
   - `determine()` → "bull" | "bear" | "neutral"
   - 输入: northbound_sentiment + 上证日线
   - 连续N天判定逻辑

2. `SystemConfig` 新增双套参数
   - `regime_mode: str = "auto"` (auto / bull / bear / neutral)
   - 每套参数用 `_bull` / `_bear` 后缀区分

3. `get_screening_configs()` 根据 regime 选择参数

### Phase 2: 阈值校准

4. 回测验证两套阈值的差异
5. 实盘观察 `active_buy_ratio` 分布后校准

### Phase 3: 优化

6. 增加更多市场状态因子（板块轮动、涨跌比）
7. 支持手动切换模式（`--regime bull` CLI 参数）

## 当前讨论进度

- [x] 双阈值方向确认
- [ ] 牛熊判定因子确认
- [ ] 阈值差异值确认
- [ ] 实施
