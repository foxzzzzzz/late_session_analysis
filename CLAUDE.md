# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A股尾盘分析系统 (A-stock Late Session Analysis) — scans 5000+ stocks during 14:30-15:00 to identify intraday trading opportunities for T+0 buy (late session) / T+1 sell (next morning) strategy.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_layer1_access.py -v

# Run a single test function
pytest tests/test_layer1_access.py::TestLayer1Access::test_st_stock_filtered -v

# === 实盘管线 ===
# Quick test mode (run full pipeline with live market data, any time of day)
python main.py --test

# Dry-run (fetch data only, no screening or report)
python main.py --test --dry-run

# Disable LLM, rule-scoring only
python main.py --test --no-llm

# Real-time mode (only works 14:25-15:05 on trading days)
python main.py

# Scheduled mode (auto-trigger at 14:29 daily)
python main.py --schedule

# === 回测 ===
# Full backtest with cache
python main_backtest.py --start 20260401 --end 20260515

# Cold run (disable cache)
python main_backtest.py --start 20260401 --end 20260515 --no-cache

# Override thresholds via env
BT_KLINE_MIN_YANG_RATIO_4D=0.50 BT_KLINE_MIN_CONSECUTIVE_CLOSE_RISE=2 \
  python main_backtest.py --start 20260301 --end 20260401
```

## Architecture

The system is a **5-stage sequential funnel pipeline**:

```
S0: Sector Prefilter (同花顺涨幅排序 → Top 3-5板块 → ~1000 stocks)
    ↓
S1: L1 (access) + K-line (morphology): ~1000 → 50-200 stocks
    ↓
S2: L2 (anomaly) + 5-min bars: 50-200 → 15-50 stocks
    ↓
S3: L3 (technical) + capital flow enrichment: 15-50 → 5-25 stocks
    ↓
S4: L4 (scoring) + LLM merge: Top 30 → final recommendations
    ↓
Jinja2 Markdown report
```

**`StockContext`** (`screening/context.py`) is the unified data structure that flows through all layers.

### Screening layers

- **S0** (`data_provider/sector_filter.py`): Sector prefilter — 同花顺行业涨幅排名, Top 3-5 sectors, outputs ~1000 candidates. Falls back to baostock keyword matching when akshare unavailable.
- **L1** (`screening/layer1_access.py`): Base filters — ST/suspended/limit-up exclusion, turnover ≥ 50M, turnover rate ≥ 1%, price ≥ ¥5.
- **K-line** (`screening/layer_kline.py`): 7 R1 checks (atr_range, consecutive_up, up_frequency, yang_ratio, close_momentum, single_day_pct, pct_vs_atr) + 6 R2 warnings. R1 eliminates, R2 warns only.
- **L2** (`screening/layer2_anomaly.py`): Late-session anomaly detection — volume gate (OR) + price-pattern gate (rally/steady/breakout, OR) + capital gate (AND). require_capital=True is the default; auto-relaxes when pass < l2_min_pass.
- **L3** (`screening/layer3_technical.py`): Technical verification — MA alignment, history_win_rate (≥40%, 5-day), vol_ratio [1.1,2.0], volatility ≤0.50, consecutive_limit_ups, sector rank, bad news/unlock date filtering, volume_shrinking. Returns per-condition fail distribution in logs.
- **L4** (`screening/layer4_scoring.py`): 100-point composite — A: tail strength (30), B: K-line form (25), C: capital flow (20), D: MA system (15), E: market environment (10). Always uses standard weights. Recommendation thresholds auto-adjust based on data availability (4 scenarios: fund flow ± LLM).

### LLM integration (`analysis/`)

Uses LiteLLM for provider-agnostic LLM calls. Parallel execution (ThreadPoolExecutor, max 30 workers, 15s timeout per stock). LLM results merged with rule scores at 30/70 weight via `merger.py`. On LLM failure, `rule_scorer.py` provides fallback — the system never blocks on LLM availability.

### Data providers (`data_provider/`)

Priority: `tencent → sector_based → sina → efinance → akshare`. Falls back on failure. Key providers:
- **TencentFetcher**: Primary real-time quotes (PE/PB/市值), batch by 50 codes per page
- **Preloader** (`preloader.py`): Loads static data before trading window — 同花顺行业对比, 限售解禁, 概念标签, mootdx K线. Runtime ~1.8s.
- **KlineProvider** (`kline_provider.py`): Daily + 5-min K-line from mootdx. Includes `compute_position_20d()` for 20-day price percentile.
- **Capital flow**: 新浪 (fund flow) + 东财 (minute flow), merged with fallback. Max enrich 300 stocks (configurable).
- **Northbound** (`northbound.py`): 同花顺 hsgtApi 北向情绪, self-cached.
- **Concept** (`concept_analyzer.py`): 同花顺概念频次热度分析.

### Backtest system (`backtest/`)

Historical replay of the full pipeline using baostock daily + 5-min bars. Key design decisions:
- **Synthetic data estimation** (`backtest/synthetic_data.py`) — capital flow (big_order_net/ratio, active_buy_ratio) estimated from 5-min bar direction×amount; bid_vol/ask_vol from last bar's close position; concepts from sector name. L4 C dimension no longer zero.
- **L4 thresholds** — strong_buy≥55, buy≥48, watch≥38. Scores with synthetic data typically range 40-70 (vs 30-39 without).
- **L3 volatility relaxed** — max_volatility 0.60 (vs 0.50 live), reduces volatility eliminations in backtest
- **14:59 cutoff** on 5-min bars — captures full late session including 14:50-15:00 peak volume
- **K-line thresholds relaxed** — yang_ratio 0.50 (vs 0.75 live), close_momentum 2 days (vs 3 live) — to generate sufficient signals for strategy validation
- **Stop-loss/take-profit** at ±5% — priority: stop_loss > take_profit > next_open
- **No LLM** — `llm_results={}`, `rule_weight=1.0`. LLM data would introduce lookahead bias.
- **No S0** — uses fixed `TARGET_SECTORS` components directly (`skip_s0=True`)
- **Sector performance backfilled** — computed from daily snapshot per sector
- **parquet disk cache** — daily bars with date-range validity check; 5-min bar cache per stock per day
- **Non-trading hours** — skips akshare, uses baostock directly

Backtest vs live differences documented in `doc/backtest_vs_live_20260523.md`.

### Configuration

Three-tier hierarchy: `.env` → `SystemConfig` defaults → layer dataclass defaults.
- **`orchestration/config.py`**: `SystemConfig` — all screening thresholds, LLM settings, data provider priorities
- **`backtest/config.py`**: `BacktestConfig(SystemConfig)` — inherits live thresholds, overrides K-line/L4 defaults for backtest. All overrideable via `BT_*` env vars.

### Key files

| Path | Role |
|------|------|
| `main.py` | Live pipeline entry point |
| `main_backtest.py` | Backtest entry point |
| `orchestration/pipeline.py` | Live pipeline orchestrator (S0→S4 loop) |
| `orchestration/config.py` | SystemConfig — all thresholds |
| `screening/context.py` | StockContext dataclass |
| `screening/layer_kline.py` | K-line morphology filter (R1+R2) |
| `backtest/engine.py` | Backtest engine (day loop + S1→S4) |
| `backtest/data_loader.py` | baostock daily/5min loader + cache |
| `backtest/data_adapter.py` | Historical data → StockContext |
| `backtest/config.py` | BacktestConfig with BT_* overrides |
| `backtest/performance.py` | Win rate, Sharpe, Calmar, drawdown |
| `backtest/report_generator.py` | CSV + JSON + Markdown export |
| `backtest/synthetic_data.py` | Synthetic capital flow / order book / concept estimation from 5-min bars |
| `data_provider/kline_provider.py` | mootdx K-line + position_20d |
| `data_provider/tencent_fetcher.py` | Tencent real-time API (PE/PB/市值) |
| `data_provider/sector_filter.py` | S0 sector prefilter |
| `screening/layer2_anomaly.py` | L2 anomaly detection |
| `screening/layer4_scoring.py` | L4 5-dimension scoring |

## Key Patterns

- Tests use factory functions (`make_ctx(**kwargs)`) to create `StockContext` instances with sensible defaults.
- All screening functions are stateless: take list of contexts + config, return filtered list, set pass/fail flags on each context.
- Capital flow and orderbook data are optional but enabled by default. L2 auto-relaxes capital gate when pass count < `l2_min_pass`.
- LLM calls use `LLM_API_KEY` from env. If unset, system transparently switches to pure rule-scoring mode.
- Backtest thresholds differ from live — see `doc/backtest_vs_live_20260523.md` for the current diff.
