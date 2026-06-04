# Live Replay Data Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist live-visible decision data and add replay-aware backtesting, with tests for snapshotting, cache behavior, 5-minute fetch resilience, fund-flow metadata, and decision-time truncation.

**Architecture:** Add a focused `SnapshotStore` for JSONL snapshot I/O, wire it into the live pipeline at stage boundaries, then update data providers and backtest config/engine without changing existing public call sites. Historical backtests remain default, while replay/proxy modes are explicit.

**Tech Stack:** Python, pytest, dataclasses, JSONL files, existing pandas/mootdx/Tencent provider interfaces.

---

### Task 1: Snapshot Store

**Files:**
- Create: `data_provider/snapshot_store.py`
- Test: `tests/test_snapshot_store.py`

- [ ] Write tests proving a record can be written and read by date/stage.
- [ ] Implement `SnapshotStore` with `write_stage_snapshot`, `read_stage_snapshots`, and `build_record`.
- [ ] Run `python -m pytest tests/test_snapshot_store.py -q -p no:cacheprovider`.

### Task 2: Live Pipeline Snapshot Writes

**Files:**
- Modify: `orchestration/config.py`
- Modify: `orchestration/pipeline.py`
- Test: `tests/test_pipeline_snapshots.py`

- [ ] Add config fields `enable_live_snapshots` and `live_snapshot_dir`.
- [ ] Add small serialization helpers for `StockContext`, quote lists, metrics, and filter results.
- [ ] Write S2/S4 unit tests using a fake store to prove snapshots include stage, iteration, codes, metrics, and relaxed-capital flag.
- [ ] Wire snapshot writes into S1/S2/S3/S4 without failing the live pipeline on snapshot write errors.
- [ ] Run `python -m pytest tests/test_pipeline_snapshots.py -q -p no:cacheprovider`.

### Task 3: Tencent Short TTL Cache

**Files:**
- Modify: `data_provider/tencent_fetcher.py`
- Test: `tests/test_tencent_fetcher.py`

- [ ] Add cache fields initialized from `TENCENT_FETCH_CODES_TTL_SECONDS`, default `3`.
- [ ] Cache normalized sorted `fetch_codes` requests and return copied quotes.
- [ ] Test repeated calls inside TTL call `_fetch_batch` only once.
- [ ] Run `python -m pytest tests/test_tencent_fetcher.py -q -p no:cacheprovider`.

### Task 4: Concurrent 5-Minute Batch Fetch

**Files:**
- Modify: `data_provider/kline_provider.py`
- Test: `tests/test_kline_provider.py`

- [ ] Add bounded concurrency to `load_5min_batch` with a one-retry per-code helper.
- [ ] Test one code failure does not block another code result.
- [ ] Run `python -m pytest tests/test_kline_provider.py -q -p no:cacheprovider`.

### Task 5: Fund-Flow Metadata

**Files:**
- Modify: `orchestration/pipeline.py`
- Test: `tests/test_fund_flow_metadata.py`

- [ ] Attach `fund_flow_source`, `fund_flow_data_date`, `fund_flow_fetched_at`, `fund_flow_is_realtime`, and `fund_flow_sources_seen` to `ctx.data_quality_flags`.
- [ ] Test minute/Sina current-day data is marked realtime and push2his stale data is not treated as realtime.
- [ ] Run `python -m pytest tests/test_fund_flow_metadata.py -q -p no:cacheprovider`.

### Task 6: Backtest Modes and Decision-Time Truncation

**Files:**
- Modify: `backtest/config.py`
- Modify: `backtest/engine.py`
- Modify: `main_backtest.py`
- Test: `tests/test_backtest_engine.py`

- [ ] Add `backtest_type`, `decision_time`, and `live_snapshot_dir` config/CLI fields.
- [ ] Replace hard-coded `14:59` cutoff with configured `decision_time`.
- [ ] Add `proxy` mode that fills fund-flow-like fields from 5-minute metrics and marks metadata as proxy.
- [ ] Add explicit `live_replay` guard that fails clearly when snapshots are missing.
- [ ] Test cutoff and mode routing.
- [ ] Run `python -m pytest tests/test_backtest_engine.py -q -p no:cacheprovider`.

### Task 7: Reporting Labels

**Files:**
- Modify: `backtest/report_generator.py`
- Test: `tests/test_backtest_report.py`

- [ ] Include `backtest_type`, `decision_time`, and `capital_flow_mode` in summary and overview reports.
- [ ] Test reports include `historical_backtest` or `live_replay_backtest`.
- [ ] Run `python -m pytest tests/test_backtest_report.py -q -p no:cacheprovider`.

### Task 8: Final Verification

**Files:**
- All modified files

- [ ] Run focused tests for all new behavior.
- [ ] Run the existing relevant offline suite:
  `python -m pytest tests/test_kline_provider.py tests/test_layer2_anomaly.py tests/test_layer4_scoring.py tests/test_backtest_engine.py tests/test_market_regime.py tests/test_llm_analysis.py -q -p no:cacheprovider`
- [ ] Run `git diff --stat` and summarize changed files.
