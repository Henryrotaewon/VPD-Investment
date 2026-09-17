# MAGI1 — Crypto Shock Flow

Run `python -m magi1.runner`. The only mode is `MAGI1_MODE=COLLECT_ONLY`.
No order endpoints or account credentials are used. VPD and MAGI2 are independent.

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
- Address labels are kept in `/data/magi1/address_labels.jsonl` as auditable evidence:
  chain/address/entity/role, evidence grade, source, `known_at_ms`, and validity window.
  Historical classification uses labels known by the event time; later discoveries are
  available only through an explicitly separate reclassification view. Conflicting active
  labels remain `CONFLICT` and are excluded from entity-level causal claims.
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
