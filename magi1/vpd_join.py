from __future__ import annotations

import json
from bisect import bisect_right
from pathlib import Path
from typing import Any


class VPDPointInTimeJoin:
    """As-of join: only snapshots at or before the shock timestamp are visible."""
    def __init__(self, snapshots: list[dict[str,Any]]):
        self.rows=sorted(snapshots,key=lambda x:x["ts_ms"]); self.times=[x["ts_ms"] for x in self.rows]

    @classmethod
    def from_jsonl(cls,path: str):
        p=Path(path); return cls([json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else [])

    def join(self, ts_ms: int, asset: str, price_reaction_bps: float | None=None):
        i=bisect_right(self.times,ts_ms)-1
        if i<0:return {"vpd":None,"delta_vpd":None,"momentum":None,"price_non_reaction":None}
        item=self.rows[i].get("assets",{}).get(asset,{})
        return {"vpd":item.get("vpd"),"delta_vpd":item.get("delta_vpd"),"momentum":item.get("momentum"),"price_non_reaction":abs(price_reaction_bps or 0)<2 if price_reaction_bps is not None else None,"vpd_ts_ms":self.rows[i]["ts_ms"]}
