"""DKA accumulation replay, 2026-08-19 through 2026-08-27 KST.
Validation only. No Telegram. Reuses the anti-lookahead single-market VPD replay engine.
"""
from point_in_time_replay import analyse, KST
from datetime import datetime, timedelta
from pathlib import Path
import json

START=datetime(2026,8,19)
END=datetime(2026,8,27)
CHECKPOINTS=[(7,20),(12,0),(17,50)]
rows=[]
d=START
while d<=END:
    for hh,mm in CHECKPOINTS:
        ts=d.replace(hour=hh,minute=mm,second=0)
        t=ts.replace(tzinfo=KST)
        row,err=analyse("KRW-DKA","DKA",t)
        rows.append({"asof_kst":ts.strftime("%Y-%m-%d %H:%M:%S")+" KST","result":row,"error":err})
    d += timedelta(days=1)

# Add lightweight transition diagnostics without changing VPD itself.
prev=None
for item in rows:
    r=item.get("result")
    if not r:
        item["transition"]={"delta_vpd":None,"delta_price_pct":None}
        continue
    if prev:
        dv=round(r["VPD"]-prev["VPD"],1)
        dp=round((r["price"]/prev["price"]-1)*100,2) if prev["price"] else None
    else:
        dv=dp=None
    item["transition"]={"delta_vpd":dv,"delta_price_pct":dp}
    prev=r

out={
  "name":"DKA Accumulation Point-in-Time Replay",
  "period":"2026-08-19..2026-08-27 KST",
  "method":"single-market v1.5/v1.6-score-compatible anti-lookahead replay; 07:20, 12:00, 17:50 checkpoints",
  "note":"VPD score only; VWPI/Distribution Risk not included; historical rank requires a separate full-universe replay.",
  "rows":rows
}
Path("data/dka_accumulation_replay.json").write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(out,ensure_ascii=False,indent=2))
