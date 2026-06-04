# Live Replay Data Layer Design

## Goal

Upgrade `late_session_analysis` so live trading runs preserve the data that was actually visible at each decision point, and so backtests can clearly report two different meanings:

- `historical_backtest`: approximate historical validation using historical daily and 5-minute bars.
- `live_replay_backtest`: replay of captured live snapshots, starting only from dates where snapshots exist.

This keeps long-history parameter research separate from real live-decision validation.

## Non-Goals

- Do not replace the current live screening strategy.
- Do not introduce a database service.
- Do not make LLM output a primary trading signal.
- Do not require paid data providers.

## Architecture

Add a small snapshot persistence layer:

`data_provider/snapshot_store.py`

Responsibilities:

- Create a `run_id` for each live pipeline run.
- Write per-stage, per-iteration snapshot records.
- Read snapshots back by date, stage, and run id for replay.
- Keep storage append-friendly and inspectable.

Default storage root:

`live_snapshots/YYYYMMDD/<stage>/<HHMMSS>_round<N>.jsonl`

The format is JSON Lines. Each line is one record. JSONL is chosen because it is easy to inspect, append, diff, and recover from partial writes. If a later implementation needs Parquet for performance, it can be added as a secondary export without changing the logical record shape.

## Snapshot Record

Each record contains:

- `schema_version`: integer, initially `1`.
- `run_id`: unique id for the pipeline run.
- `trading_date`: `YYYYMMDD`.
- `stage`: `S1`, `S2`, `S3`, or `S4`.
- `iteration`: integer loop counter.
- `fetched_at`: local timestamp with seconds.
- `decision_time`: configured decision cutoff where relevant.
- `codes`: codes requested in that stage iteration.
- `quotes`: normalized realtime quote payloads used to build `StockContext`.
- `late_metrics`: computed 5-minute tail metrics by code.
- `fund_flow`: raw and normalized fund-flow values by code.
- `filter_result`: input count, output count, passed codes, failed codes if available, and flags such as `relaxed_capital`.
- `data_quality`: source names, availability flags, data dates, realtime flags, and errors.

Large raw provider responses may be trimmed to fields used by the strategy. The snapshot must preserve every value that can affect filtering or scoring.

## Live Pipeline Changes

`orchestration/pipeline.py` will initialize a `SnapshotStore` when live snapshotting is enabled.

S1 writes after quote fetch, daily enrichment, L1, and K-line screening.

S2 writes once per loop after quote fetch, 5-minute metrics, fund-flow enrichment, and L2 screening. If the minimum-pass safeguard relaxes capital requirements, the record writes `filter_result.relaxed_capital=true`.

S3 writes after refreshed quotes, 5-minute metrics, fund-flow refresh, news/risk enrichment, leader enrichment, and L3 screening.

S4 writes once per scoring loop after refreshed quotes, refreshed 5-minute metrics, L4 scoring, and before LLM merge. A final S4 merge snapshot may also record recommendation thresholds and merged recommendations.

Snapshot failures must not break live screening. They are logged and stored as data-quality errors when possible.

## Tencent TTL Cache

`data_provider/tencent_fetcher.py` will add a short in-memory TTL cache for `fetch_codes`.

Defaults:

- Enabled by default.
- TTL: 3 seconds.
- Environment override: `TENCENT_FETCH_CODES_TTL_SECONDS`.
- `0` disables the cache.

Cache key is the sorted, normalized tuple of requested codes. Cached results are copied before returning to prevent accidental mutation.

This reduces repeated S2/S4 requests without hiding meaningful market changes because S4 refreshes every 10 seconds by default.

## 5-Minute K-Line Batch Fetch

`data_provider/kline_provider.py` will keep the public `load_5min_batch(codes, bars=48)` API but implement bounded concurrency.

Defaults:

- `max_workers`: 4.
- Per-code retry: 1 retry after a short delay.
- Single-code failures do not fail the batch.
- Results remain a `dict[code, DataFrame]`.

This preserves current callers while reducing stage latency for 200-500 candidates.

## Fund-Flow Metadata

Fund-flow enrichment will attach metadata to `StockContext.data_quality_flags`.

Fields:

- `fund_flow_source`: `minute`, `sina`, `push2his`, or `none`.
- `fund_flow_data_date`: provider data date if available.
- `fund_flow_fetched_at`: local timestamp.
- `fund_flow_is_realtime`: true for current intraday minute/Sina data, false for historical or degraded data.
- `fund_flow_sources_seen`: list of successful provider sources.

Scoring continues to consume the existing numeric fields first. Metadata is added so future scoring can decay stale data without another data-layer migration.

## Backtest Modes

`BacktestConfig` adds:

- `backtest_type`: `historical` or `live_replay`.
- `decision_time`: default `14:57`.
- `capital_flow_mode`: `none`, `proxy`, or `replay`.
- `live_snapshot_dir`: default `./live_snapshots`.

`historical_backtest` behavior:

- Uses historical daily and 5-minute bars.
- Truncates 5-minute bars to `decision_time`, not to `14:59`.
- Reports as `historical_backtest`.
- Supports `capital_flow_mode=none` and `proxy`.

`proxy` mode:

- Estimates fund-flow-like fields from 5-minute volume/price behavior.
- Marks metadata as `fund_flow_source=proxy` and `fund_flow_is_realtime=false`.
- Keeps report output visibly separate from real fund-flow data.

`live_replay_backtest` behavior:

- Reads captured live snapshots.
- Reconstructs stage outputs from recorded visible data.
- Supports `capital_flow_mode=replay`.
- Skips dates without snapshots or reports them as unavailable.
- Reports as `live_replay_backtest`.

If `backtest_type=live_replay` is requested without usable snapshots, the run should fail clearly or produce an empty report with an explicit reason. It must not silently fall back to historical approximation.

## Reporting

Backtest reports include:

- `backtest_type`.
- `decision_time`.
- `capital_flow_mode`.
- Snapshot coverage if replay mode is used.
- Whether L2 capital relaxation occurred.

This prevents comparing historical approximation and live replay as if they were the same metric.

## Error Handling

- Snapshot write failure logs a warning and does not interrupt live screening.
- Snapshot read failure in replay mode is fatal for that replay run.
- Tencent cache failures fall back to direct fetch.
- 5-minute per-code failures are recorded and skipped.
- Fund-flow stale data is preserved as metadata but not treated as realtime.

## Testing

Add focused tests for:

- SnapshotStore writes and reads JSONL records.
- S2/S4 snapshot records contain stage, iteration, codes, metrics, and filter result.
- `decision_time=14:55` truncates historical 5-minute bars after that time.
- Tencent `fetch_codes` TTL cache avoids repeated `_fetch_batch` calls within the TTL.
- 5-minute batch fetch returns successful codes when one code fails.
- Fund-flow metadata distinguishes realtime and degraded sources.
- `capital_flow_mode` routes `none`, `proxy`, and `replay` distinctly.
- Reports include `historical_backtest` or `live_replay_backtest`.

## Rollout

Implement in small steps:

1. SnapshotStore and live snapshot writes.
2. Tencent TTL cache.
3. Concurrent 5-minute fetch.
4. Fund-flow metadata.
5. Historical `decision_time` truncation and `proxy` mode.
6. Live replay loader and reporting.

Each step should preserve existing CLI defaults unless explicitly configured.
