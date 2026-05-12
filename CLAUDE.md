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

# Quick test mode (run full pipeline with current market data, any time of day)
python main.py --test

# Dry-run (fetch data only, no analysis or report)
python main.py --test --dry-run

# Disable LLM, rule-scoring only
python main.py --test --no-llm

# Run specific pipeline stages only
python main.py --test --stages 1,2

# Real-time mode (only works 14:25-15:05 on trading days)
python main.py

# Scheduled mode (auto-trigger at 14:29 daily)
python main.py --schedule
```

## Architecture

The system is a **4-stage sequential funnel pipeline** that progressively narrows ~5000 stocks to ~5-10 actionable picks:

```
Data Providers (efinance → akshare fallback)
    ↓
[Stage 1] L1 (access) + L2 (anomaly): 5000 → 100-200 stocks
[Stage 2] L3 (technical verification): 100-200 → 30-50 stocks
[Stage 3] L4 (quantitative scoring): 30-50 → Top 30, scored 0-100
[Stage 4] LLM analysis + rule-score merge: Top 30 → final recommendations
    ↓
Jinja2 report (Markdown, optional PNG via imgkit)
```

**`StockContext`** (`screening/context.py`) is the unified data structure that flows through all layers. Each layer reads/writes its own fields on the same object. Layers set `lN_passed` flags; later stages read `total_score` and `anomaly_type` from earlier layers.

**4 screening layers:**
- **L1** (`layer1_access.py`): Base filters — ST/suspended/limit-up exclusion, turnover > 50M, turnover rate > 1%, price ¥5-100
- **L2** (`layer2_anomaly.py`): Late-session anomaly detection — volume surge, price rally/breakout/stabilization, optional capital flow and orderbook checks (disabled in MVP)
- **L3** (`layer3_technical.py`): Technical/market context — MA alignment, historical win rate, volatility, sector strength, bad news/ unlock date filtering
- **L4** (`layer4_scoring.py`): 100-point composite score — tail strength (35%), technical (25%), capital (20%), market environment (15%), history (5%)

**LLM integration** (`analysis/`): Uses LiteLLM for provider-agnostic LLM calls. Stage 4 calls LLM in parallel (ThreadPoolExecutor, max 30 workers, 15s timeout per stock). LLM results are merged with rule scores at 30/70 weight via `merger.py`. On LLM failure, `rule_scorer.py` provides a rule-based fallback — the system never blocks on LLM availability.

**Data providers** (`data_provider/`): `DataFetcherManager` tries fetchers in priority order (efinance → akshare → pytdx). On failure, automatically degrades to the next available source. Each fetcher wraps a third-party library and normalizes output to `RealtimeQuote` dataclasses.

**Preloading** (`data_provider/preloader.py`): Before the trading window, loads static data (sector mappings, sector performance) via akshare to reduce in-session latency.

**Configuration** (`orchestration/config.py`): `SystemConfig` dataclass reads from environment variables (via `python-dotenv`). Screening thresholds, LLM settings, and data provider priorities are all configurable via `.env`. See `.env.example` and `LLM_config.md` for all options.

**Report** (`report/`): Jinja2 templates render Markdown reports. `report.j2` is the main report template (recommendations + stats summary); `stock_card.j2` renders individual stock detail cards.

## Key Patterns

- Tests use factory functions (`make_ctx(**kwargs)`) to create `StockContext` instances with sensible defaults — follow this pattern when adding new tests.
- All screening functions are stateless: they take a list of contexts + config, return a filtered list, and set pass/fail flags on each context object.
- MVP phase: capital flow data (`require_capital`) and orderbook data (`require_orderbook`) are disabled by default in L2 because the primary data sources don't consistently provide them.
- LLM calls use `LLM_API_KEY` from env to determine availability. If unset, the system transparently switches to pure rule-scoring mode.
