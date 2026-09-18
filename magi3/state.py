import json
from dataclasses import asdict
from pathlib import Path

class ShadowLedger:
    """MAGI3-only ledger. Never reads/writes MAGI1 or MAGI2 state."""
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.events=self.root/"shadow_events.jsonl"
    def append_fill(self,signal_id,market,fill,ts_ms):
        row={"event":"SHADOW_FILL","signal_id":signal_id,"market":market,"ts_ms":ts_ms,**asdict(fill)}
        with self.events.open("a",encoding="utf-8") as f:f.write(json.dumps(row,separators=(",",":"))+"\n")
        return row
