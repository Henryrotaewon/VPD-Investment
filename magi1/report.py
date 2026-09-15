from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


KST=timezone(timedelta(hours=9))

def build_daily(storage, now=None, output_dir=None):
    now=now or datetime.now(KST); end=now.replace(hour=7,minute=0,second=0,microsecond=0)
    if now<end:end-=timedelta(days=1)
    start=end-timedelta(days=1); states=storage.query("flow_state",int(start.timestamp()*1000),int(end.timestamp()*1000)); chains=storage.query("shock_chain",int(start.timestamp()*1000),int(end.timestamp()*1000))
    cohorts=Counter(x.get("cohort","unclassified") for x in chains)
    lines=[f"# MAGI1 Daily Crypto Shock Report — {end:%Y-%m-%d} 07:00 KST","","> Research/COLLECT_ONLY. No order execution.","","## Reconstruction order","","On-chain → Global → Derivatives → Korea → VPD → Price","",f"Observed state events: **{len(states)}**"]
    for s in states[-20:]: lines.append(f"- {s['asset']} {s['direction']} — {s['state']} / origin={s.get('origin_venue')} / confidence={s.get('origin_confidence')}")
    lines += ["","## Cohort comparison","", "| Cohort | Samples |","|---|---:|",*(f"| {x} | {cohorts.get(x,0)} |" for x in ("FLOW_ONLY","VPD_ONLY","FLOW_VPD","FLOW_VPD_NON_REACTION"))]
    text="\n".join(lines)+"\n"; root=Path(output_dir or storage.root/"reports"); root.mkdir(parents=True,exist_ok=True); path=root/f"magi1_daily_{end:%Y%m%d}.md"; path.write_text(text,encoding="utf-8"); return path
