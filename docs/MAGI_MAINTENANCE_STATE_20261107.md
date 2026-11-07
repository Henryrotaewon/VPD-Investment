# MAGI maintenance checkpoint — 2026-11-07

## Operational decision
At the user's direction, all three Railway MAGI services were paused before further cleanup. **No simulated or live trading, data collection, or signaling should be restarted until a separate restoration review.** Service resources and volume mounts were retained.

| Service | Railway service ID | Data mount | Current start command | Restart policy |
|---|---|---|---|---|
| MAGI1-Flow | `c58d9b91-e9c5-481d-9242-3d89ee75ed37` | `/data` (5GB) | `python -c 'import sys; sys.exit(0)'` | `NEVER` |
| MAGI2-Strategy | `6faa0028-de22-4703-b308-d8d165c0ef26` | `/data/magi2` (1GB) | `python -c 'import sys; sys.exit(0)'` | `NEVER` |
| MAGI3-Execution | `1cd88d49-0c14-405a-b3de-a280a456dbe6` | `/data/magi3` (500MB) | `python -c 'import sys; sys.exit(0)'` | `NEVER` |

- Project: `1a5ba1e7-187e-4591-8141-7e048ed416d0`, production environment `f0bfd512-4fbc-46ae-aa17-c169907452cf`.
- Temporarily restricted GitHub auto-deploy watch patterns to nonexistent files (manual-resume guard). Do not claim a new commit will deploy until the patterns are restored.
- Original/expected service entrypoints based on repository docs: MAGI1 `python -m magi1.runner`, MAGI2 `python magi2/server_runner.py`, MAGI3 `python -m magi3.runner`. For MAGI1/MAGI3 this is the repository-documented command, **not an independently captured pre-maintenance Railway config snapshot**. Verify the current deployment setup at restart.
- Resume process should restore startCommand, appropriate actual watchPatterns, desired restart policy, and explicitly trigger a fresh deployment; then verify behavior in logs. Do not reuse earlier successful deployment as proof of restored state.

## FAST-only storage cleanup
- Four `fast-observe/**/observe.sqlite3` databases plus their exact WAL/SHM companions removed while application workers were stopped. 655,267,406 file bytes removed and filesystem free space grew from 0 to 655,429,632 bytes, Railway maintenance logs 2026-11-07.
- Detailed FAST signal/tick/short-window observations cannot be replayed after this removal. Preserve source code and experiment summary at `docs/FAST_BASE_ARCHIVE_SUMMARY_20261107.md`.
- Four `paper-v1.sqlite3` ledgers, other strategy SQLite files (derivatives/BEAR, VPD/indicator, Bollinger, capture audit, Shadow), and the original `paper_state.json` remain on the volume.
- Post-cleanup FAST ledger totals: Upbit 93 closed/24 wins/1 simulated holding; Bithumb 53/8/0; Binance 137/23/5; Kraken 0/0/0. Currency-specific realized P&L is recorded in the FAST summary; no cross-currency mixing.
- MAGI2 VPD `paper_state.json` still fails JSON decode at line 813 column 23; original file unchanged with SHA256 `f12cd5ef1a36d7bb45f885a9f12e7f4a37c6704971c6751e4c2906f6dc98d5c7`. The `paper-history` branch has an older 2026-10-27 state snapshot, **not a verified substitute** for the later live file.
- MAGI1 Google Drive archive OAuth last showed `invalid_grant`, so do not delete MAGI1 unverified research/raw data. MAGI3 was in `DRY_RUN` and showed `UNAVAILABLE` feed due to MAGI2 before pause.

## Next safe recovery order
1. Keep all three services stopped; preserve original corrupted VPD state and paper event log by verified, external backup.
2. Validate whether VPD state can be repaired from the events and an independently verified snapshot without overwriting evidence. Preserve status of pending positions.
3. Review archived FAST performance and decide if suspended simulated holdings need explicit account-level end-state handling.
4. Test MAGI2 on a cloned/isolated data set, then resume the main MAGI2 service after correctness and durability checks; re-enable MAGI1/MAGI3 only as requested.
