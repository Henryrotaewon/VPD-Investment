from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Storage:
    """Persistent append-only raw JSONL plus indexed research SQLite."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.raw = self.root / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.root / "research.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, ts_ms INTEGER, asset TEXT, payload TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_records ON records(kind, ts_ms, asset)")
        self.db.commit()

    @staticmethod
    def payload(value: Any) -> dict[str, Any]:
        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
        return dict(value)

    def append_raw(self, kind: str, value: Any) -> None:
        row = self.payload(value)
        ts = row.get("received_ts_ms") or row.get("event_ts_ms") or 0
        day = __import__("datetime").datetime.fromtimestamp(ts / 1000, __import__("datetime").timezone.utc).strftime("%Y-%m-%d")
        path = self.raw / f"{kind}-{day}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def append(self, kind: str, value: Any) -> None:
        row = self.payload(value)
        ts = int(row.get("event_ts_ms") or row.get("received_ts_ms") or row.get("origin_ts_ms") or 0)
        asset = row.get("asset") or row.get("base")
        with self._lock:
            self.db.execute("INSERT INTO records VALUES(?,?,?,?)", (kind, ts, asset, json.dumps(row, ensure_ascii=False)))
            self.db.commit()

    def query(self, kind: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload FROM records WHERE kind=? AND ts_ms>=? AND ts_ms<? ORDER BY ts_ms", (kind, start_ms, end_ms))
        return [json.loads(x[0]) for x in rows]

    def health(self) -> dict[str, Any]:
        return {"root": str(self.root), "free_bytes": os.statvfs(self.root).f_bavail * os.statvfs(self.root).f_frsize}
