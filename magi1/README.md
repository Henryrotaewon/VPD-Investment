# MAGI1 — Crypto Shock Flow

Run `python -m magi1.runner`. The only mode is `MAGI1_MODE=COLLECT_ONLY`.
No order endpoints or account credentials are used. VPD and MAGI2 are independent.


## Project consolidation — 2026-09-16

**MAGI1 Wave (this `magi1/` package in `Henryrotaewon/VPD-Investment`) is the
canonical implementation for market-data collection, flow formation and
cross-exchange propagation research.** The separate `MAGI-Microstructure` POC is
retired as a development track; its repository deletion has been requested.
This section preserves the research handoff independently of that repository.
It records planned work, not completed migration or verified trading performance.

VPD remains the independent background scanner and MAGI2 remains the independent
existing paper strategy. Future MAGI1 executable-price experiments must use their
own ledger and must not change MAGI2 positions or VPD scoring.

### Features to carry forward

| Priority | Research item | Preserved design and required work | Status |
|---|---|---|---|
| 1 | Executable bid/ask outcome labels | Long entry at available ask; exit at subsequent bid. Evaluate at 100/250/500ms, 1/2/5/10/30/60/300s only where timestamp and book resolution support it. Record gross and net returns, spread, fees, slippage, fill size and data coverage separately. Add depth-based fills, decision latency and bounded timestamp matching; missing/stale books produce missing outcomes. | Planned. Current MAGI1 price-response evaluation is gross and is not executable PnL. |
| 2 | ALP / Micro Fingerprints | Preserve trade burst, volume-price dislocation, buy/sell alternation and book imbalance as candidate features. Normalize by asset/venue baseline, require warm-up and healthy data, and attach a feature vector to the existing shock ID. Research repeated price/size and cadence separately. | Planned. Old scores were heuristics, not validated predictive signals. |
| 2 | Absorption and quote replenishment | Measure aggressive traded volume against retained book state and subsequent replenishment. Distinguish cancellations, missing updates and genuine replenishment. | New implementation required: the POC fields were zero-valued placeholders. |
| 3 | Quote Shadow / Dependency Break | Compare time-aligned quote returns across venues, estimate lag and direction agreement, and detect a decline from a stable dependency baseline. Separate KRW/USD/USDT quote effects and spot/derivative basis. Prevent future information in features and test out of sample. | Research prototype only; no calibrated causality or actor identification. |
| 3 | Tick versus persistent-flow experiments | Evaluate sub-second/second opportunities separately from seconds/minutes flow persistence. Use independent cohorts and net executable outcomes. | Planned; sub-second research requires finer validated data than the current 1s formation sampling. |

Do not migrate the POC's duplicate collector runtime, Flow Wave detector,
propagation engine or storage pipeline. Extend MAGI1's existing implementations.
Public market patterns do not prove wash trading, a shared account or a particular LP.

For historical provenance only, the retired POC was inspected at commit
`f7c146f09bccda6146d53bb7ba10998eb13aa96b`. Relevant former modules were
`src/learning/outcome_labeler.py`, `src/micro/fingerprint_engine.py` and
`src/quote_shadow/engine.py`. This README preserves requirements, not a source-code
backup; those paths need not remain accessible after repository deletion.

### Additional venue test backlog

The current six-venue spot set is Upbit, Bithumb, Coinone, Binance, Bybit and
Kraken. Kraken is already covered and is not a new integration.

| Candidate | Intended scope | Historical POC evidence | Next test |
|---|---|---|---|
| Bitget | Additional CEX spot sensor | 192 BTC trade records in the 90s observation on 2026-09-02 KST | Public spot metadata, common assets, trade sides/IDs, snapshot/delta book semantics, timestamps, reconnect and sustained feed quality |
| Aster | Separate derivative context sensor | 83 BTC trade records in that observation; POC used a futures feed | Revalidate public endpoint/subscription, instrument type, book reconstruction and timing; keep separate from spot universe and execution |
| Apex | Separate derivative context sensor | 72 BTC trade records in that observation | Revalidate public subscription, instrument identifiers, timestamp units, trade-side semantics and book continuity |
| Hyperliquid | Optional DEX market-data sensor | Listed as a POC candidate; no success claim carried forward here | Verify available public interface and exact spot/perpetual instrument mapping, then trade/book smoke tests |
| dYdX | Optional derivative market-data sensor | Listed as a POC candidate; no success claim carried forward here | Verify public interface, contract/unit mapping and trade/book stream behavior before adapter work |

Historical counts are short-run received records, not unique trades, availability
guarantees, order-book validation or evidence of a currently healthy service.
All candidates remain unenabled by this documentation change. Do not count a
derivative contract as another spot venue or introduce derivative trading.

### Promotion gates

1. Revalidate public APIs and instrument metadata from the target runner; use no private account endpoints.
2. Test trades and books separately, including IDs, aggressor side, timestamp units, sequence/checksum handling where available, deletions and reconnect resets.
3. Preserve exchange time, receive time and clock uncertainty. Missing timestamps remain missing; local detection lag is not causal lead time.
4. Require sustained coverage and report stale feeds, gaps, parse errors and clock regressions. Deduplicate episodes before statistical evaluation.
5. Add an isolated adapter only after quality checks pass; preserve comparable venue/asset cohorts when extending the current six-venue logic.
6. Promote research signals only after out-of-sample, cost-aware executable-price paper evaluation with sample counts and uncertainty. No live execution is authorized by this roadmap.

## Runtime

Six public spot collectors → bounded queue → normalized raw storage → feature buckets →
ordered formation states → point-in-time VPD cohorts → propagation observations →
matured 10s/30s/60s/300s/1800s/3600s returns/MFE/MAE/false-shock → daily report.

- Active spot metadata intersects Binance/Bybit USDT, Kraken USD and Korean KRW markets.
- Five assets are ranked by mean venue percentile of 24h traded notional; this is a
  liquidity/turnover size proxy, **not market capitalization**. All inputs are saved.
- Selection is refreshed daily. Failed discovery never silently substitutes hardcoded coins.
- Binance partial depth is a full top-10 snapshot and has no exchange timestamp;
  that field is null. Kraken ISO timestamps and book CRC32 are validated. Bybit deltas
  update retained levels and zero quantities delete levels. Reconnect resets books.
- Coinone maker flags identify the opposing aggressor; ACKs are not trades.
- Formation uses 10s/60s/600s horizons, sampled at 1s resolution, with volume/order-flow
  pressure or price/imbalance confirmation. Thresholds are research hypotheses.
- State order is FORMATION → GLOBAL_CONSENSUS → KOREA_EARLY_RECEPTION →
  PROPAGATION → NORMALIZATION. Episodes may time out before propagation. Timer-driven
  normalization works even without a subsequent trade. Shock IDs and origins stay fixed.
- Origin evidence scores are heuristic; calibrated Origin Confidence is **null**, not
  an invented probability. Reception probability has sample count and Wilson CI95.
  Lag is local detection lag, not proof of exchange causality; usable lead subtracts
  a documented 1000ms decision budget. Fewer than 30 samples are marked unreliable.
- VPD JSON and CSV are fetched read-only from one immutable GitHub commit; source time
  and local availability time both gate joins. Existing scanner formulas are untouched.
- Cohorts overlap: FLOW_ONLY; BUY VPD>=75 FLOW_VPD; plus abs(Upbit reaction)<2bps;
  independent VPD_ONLY BUY baseline once per common asset/snapshot. SELL flow outcomes
  remain separate because VPD is a bullish scanner. Returns are gross, not executable PnL.
- Outcomes mature only after their horizon. A >5s sample gap yields MISSING_DATA, not
  a fabricated zero/last-price return. Pending evaluations and deduplication checkpoints
  persist across restarts. Gaps during downtime remain missing.
- On-chain public BTC unconfirmed large transfers are candidates only, direction UNKNOWN.
  No wallet owner or smart-money identity is inferred from transfer size. Enrichment
  providers can supply verified labels through the adapter interface; commercial providers
  are not configured. Transaction output sum can include change and is not net exchange flow.
- Bybit public linear ticker provides OI/funding/mark-price context only (60s polling).
  This context does not alter spot VPD scores or create standalone signals.
- Daily reports use observed chains and matured cohort outcomes; zero samples show N/A.
  07:00 KST cutoff is persisted, and the latest missed report is generated after restart.

## Storage and operations

Railway requires a dedicated `/data` persistent volume. Files are `/data/magi1` by default.
Raw normalized trades/books are batched, gzip-compressed and rotated hourly. Default raw
retention is 2 days; research SQLite/checkpoints/reports remain. A 100MiB free-space guard
stops collection before disk exhaustion. No raw-tick GitHub writes exist.

Diagnostics every 30s include per-venue/asset trade and book counts/ages, reconnects,
parse errors, timestamp omissions/regressions, observed clock offsets, queue depth,
free space and derivative context availability. Socket connection alone is not healthy feed.

Tests: `python -m unittest discover -s magi1/tests -v`.
See `deploy/README_MAGI1_FLOW.md` for deployment settings.

Runtime repair (2026-09-16): the module entrypoint now discovers the universe and
starts the collector, with startup and shutdown logs. `--duration SECONDS` supports
bounded observations; omitted means continuous collection. Railway validates the
mounted `/data` volume before starting. Watch patterns are `/magi1/**/*.py` and
`/requirements.txt`; Markdown-only edits do not trigger deployment.

`storage_startup` and 30-second `feed_diagnostics.storage` report research counts
and latest timestamps by record kind, acknowledged raw trade/book writes since
instrumentation, retained raw file sizes, database size and free space. Raw counters
persist across restarts but are not a recount of older files or currently retained
rows; expiry does not decrease them, and a crash between gzip append and SQLite
checkpoint can undercount. Per-feed counts are process-local; research counts
come from SQLite. A successful deployment alone does not prove collection health.
