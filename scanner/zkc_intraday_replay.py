"""Fast single-market ZKC intraday replay for 2026-08-30 KST. Validation only."""
from point_in_time_replay import analyse, KST
from datetime import datetime
import json
from pathlib import Path

TIMES=[f"2026-08-30 {h:02d}:00:00" for h in range(9,19)]
# include production-relevant checkpoints
TIMES += ["2026-08-30 07:20:00","2026-08-30 14:08:00","2026-08-30 17:50:00"]
TIMES=sorted(set(TIMES))
rows=[]
for ts in TIMES:
    t=datetime.strptime(ts,"%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
    row,err=analyse("KRW-ZKC","ZKC",t)
    rows.append({"asof_kst":ts+" KST","result":row,"error":err})
out={"name":"ZKC Intraday Point-in-Time Replay","date":"2026-08-30","rows":rows}
Path("data/zkc_intraday_replay.json").write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(out,ensure_ascii=False,indent=2))
