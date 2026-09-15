# Railway — MAGI1-Flow

Create a separate Railway service from this repository. Do not replace or share the MAGI2 service process.

- Service name: `MAGI1-Flow`
- Start Command: `python -m magi1.runner`
- Environment: `MAGI1_MODE=COLLECT_ONLY`, `MAGI1_DATA_DIR=/data/magi1`, `PYTHONUNBUFFERED=1`, `TZ=Asia/Seoul`, optional `LOG_LEVEL=INFO`, optional `MAGI1_ONCHAIN_MIN_BTC=100`
- Persistent Volume mount: `/data`
- Recommended initial volume: 20 GB; monitor `free_bytes` diagnostics and expand before 20% free space.
- No GitHub raw-tick backup. Raw JSONL, SQLite research records, universe snapshots, and reports stay on the Railway volume.

The process refuses modes other than `COLLECT_ONLY`. There are no authenticated exchange clients or order endpoints.
