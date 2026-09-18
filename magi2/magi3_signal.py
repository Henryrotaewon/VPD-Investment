"""Read-only MAGI2 -> MAGI3 contract publisher. Does not modify MAGI1."""
import json,time,uuid
from pathlib import Path

def make_signal(asset,side,strategy_tags,confidence,expected_move_bps,max_holding_sec,preferred_venues=(),regime=None,metadata=None):
    tags=[str(x).upper() for x in strategy_tags]
    return {"signal_id":uuid.uuid4().hex,"created_ts_ms":int(time.time()*1000),"asset":asset.upper(),"side":side.upper(),
      "strategy":"+".join(tags),"strategy_tags":tags,"confidence":float(confidence),"expected_move_bps":float(expected_move_bps),
      "max_holding_sec":int(max_holding_sec),"preferred_venues":list(preferred_venues),"regime":regime,"metadata":metadata or {}}

def append_signal(path,signal):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("a",encoding="utf-8") as f:f.write(json.dumps(signal,separators=(",",":"))+"\n")
