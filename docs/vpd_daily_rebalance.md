# VPD 07:20 automatic PAPER rebalance

The MAGI2 Railway process owns the daily Asia/Seoul schedule. At 07:20 it calls the same `start_engine('morning')` path as the manual rebalance, pinning all markets to the 07:20 completed-minute cutoff. The existing scanner validates the snapshot before the PAPER worker may rebalance. Execution follows scan completion; 07:20 is not a guaranteed fill time.

A SQLite claim on the persistent volume is committed before dispatch, once per local calendar date. Busy workers, scan errors, process restarts and execution failures do not cause a second daily attempt. There is no 07:30 fallback and no late-start catch-up. A service that misses the entire 07:20 minute waits until the next day; manual rebalance remains available. Existing bounded HTTP retries inside a single scan are unchanged.

Initial capital, history, holdings and existing rebalance/exit rules are preserved. This is ordinary rebalance, not full replacement. PAPER-only configuration prevents scheduling LIVE orders.

The old ChatGPT 07:20 trigger and 07:30 retry tasks are disabled. The workflow no longer reacts to `.github/vpd-trigger.txt` pushes; manual administrative workflow dispatch remains available. The old GitHub scanner and point-in-time scanner share the VPD formula lineage but differ in cutoff handling, output and auxiliary indicators, so identical scores are not asserted.

The accepted scheduled snapshot is mirrored to `vpd_scheduled_snapshot.json` for the morning stored-view menu and emitted as a bounded TOP10 audit log. Result notifications continue through the existing Telegram engine. A retained read-only morning briefing must use these scheduled logs, not expect the retired GitHub scanner's morning file to update.

Activation after 07:20 does not trigger a trade that day. Startup logs show the next eligible run, and request IDs `scheduled-vpd-YYYY-MM-DD` connect claim, scan, execution and completion records. Tests cover KST/UTC conversion, the fixed cutoff, crash/restart deduplication, busy/failure/no-retry behavior, no late catch-up, PAPER gating and the shared manual scan path.
