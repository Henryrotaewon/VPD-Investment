# Railway MAGI1-Flow

Dedicated service, GitHub `Henryrotaewon/VPD-Investment`, branch `main`.

- Start command: `python -m magi1.runner`
- Persistent volume: `/data` (dedicated; never reuse MAGI2 volume)
- Variables: `MAGI1_MODE=COLLECT_ONLY`, `MAGI1_DATA_DIR=/data/magi1`,
  `PYTHONUNBUFFERED=1`, `TZ=Asia/Seoul`, `MAGI1_RAW_RETENTION_DAYS=2`,
  `MAGI1_ONCHAIN_MIN_BTC=100`, `LOG_LEVEL=INFO`
- Single replica, continuous process, sleep disabled, restart ON_FAILURE.
- Watch paths: `/magi1/**/*.py`, `/requirements.txt`.
- No public HTTP domain or HTTP healthcheck is required for this worker.
- No exchange keys, Telegram tokens or GitHub write tokens are needed.
- Code refuses Railway collection without a mounted `/data` volume.

Inspect `universe_selected`, all 60 first-event combinations (6 venues × 5 assets ×
trade/book), and `feed_diagnostics`. `SUCCESS` alone does not prove market feed health.
Raw gzip expires after configured retention; research records do not auto-expire.

`storage_startup` and `feed_diagnostics.storage` show persisted research counts,
latest timestamps, acknowledged raw writes since instrumentation, retained file
sizes and free space. Raw-write counters are not a recount of historical files.
Markdown changes are excluded from deployment watch patterns.
