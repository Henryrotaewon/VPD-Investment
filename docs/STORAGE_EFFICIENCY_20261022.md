# Storage and transfer efficiency — 2026-10-22

Keep MAGI1's 5 GB and MAGI2's 1 GB volumes. Allocated capacity is not billed
as if full; downsizing is unsupported and migration buys little at this scale.
Do not reset paper balances, delete fills, shorten evidence retention, or
transfer operational SQLite databases between services to save storage fees.

## Implemented

- Upbit FAST uses the same lossless float64 daily turnover format as other
  venues. Existing rows migrate in transactions of at most 4,096 rows;
  references combine packed and remaining rows. Unknown and zero differ.
  Ten-day lookback and recent OHLCV are preserved. No network transfer.
- Every migrated payload is decompressed and byte-checked before removing its
  original rows. Failure rolls back the batch. Restart detects packed history.
- FAST event JSON drops whitespace only; all fields and numerical values stay.
- Duplicate immutable audit evidence is rejected before JSON/zlib processing.
- MAGI1 full research snapshots run once per UTC day rather than for each raw
  batch or restart. Sealed raw segments retain checksum-verified uploads and
  verification-gated local eviction. Snapshot coverage, not live coverage,
  determines whether analytical proof exists. Research changes since the daily
  snapshot remain local until the next snapshot (up to one day of backup lag).
- Automatic remote snapshot compaction and permanent raw deletion default off.
  Explicit `MAGI1_ARCHIVE_COMPACT_REMOTE=1` or
  `MAGI1_ARCHIVE_DELETE_REMOTE=1` retain legacy opt-in paths. Do not enable them
  until remote retention and restoration have been reviewed.
- Credential failures retry hourly instead of every five minutes; other archive
  failures keep five-minute retries. Local source data remains intact on failure.
- Full MAGI1 diagnostics move from every 30 ticks to every 300 ticks; compact
  health logs remain every 30 ticks. Persisted five-minute diagnostics stay.

## Verification and operational limits

Tests cover exact ten-day ratios, zero/unknown distinction, mixed-layout reads,
restart, transactional rollback, duplicate evidence and daily backup reuse.
Synthetic benchmark: 57,600 random float64 turnover rows across 20 symbols and
10 days used 3,497,984 table/index bytes before and 835,584 after (76.11% less),
with every value exactly equal. This is not measured production disk recovery.
No production VACUUM or filesystem deletion is part of this release. SQLite
freed pages are reusable but may not reduce allocated disk bytes until a
separately planned compaction with sufficient temporary space.

Before this change: MAGI1 archive OAuth returned invalid_grant; Drive folder
discovery alone could not re-verify the historical recovery archives. Restoring
OAuth requires the same user's client/account reauthorization; successful code
deployment is not a successful backup. Confirm verified upload and restore
before claiming recovery readiness. MAGI2 indicator fill ValueError is a
separate correctness issue, not fixed by reducing log volume or storage.

Next capacity decision: measure MAGI2 after native candle repair settles, then
inspect per-DB page_count/freelist_count and WAL sizes. Keep current holdings,
pending orders, captures/outcomes, lifetime performance sources and required
lookback available. Local re-use comes before remote migration; archive only
immutable or completed records in verified batches, never repeatedly copy the
whole historical database just to reclaim a small number of bytes.
