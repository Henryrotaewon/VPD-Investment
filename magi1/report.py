"""Daily observed shock reconstruction and matured cohort comparison."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
from statistics import mean
import json
KST=timezone(timedelta(hours=9))
COHORTS=('FLOW_ONLY','VPD_ONLY','FLOW_VPD','FLOW_VPD_NON_REACTION')

def report_cutoff(now):
    end=now.astimezone(KST).replace(hour=7,minute=0,second=0,microsecond=0)
    return end if now>=end else end-timedelta(days=1)

def build_daily(storage,now=None,output_dir=None):
    end=report_cutoff(now or datetime.now(KST)); start=end-timedelta(days=1)
    lo,hi=int(start.timestamp()*1000),int(end.timestamp()*1000)
    chains=storage.query('shock_chain',lo,hi); states=storage.query('flow_state',lo,hi)
    evaluations=storage.query('evaluation',lo,hi); prop=storage.query('propagation',lo,hi)
    lines=[f'# MAGI1 Daily Crypto Shock Report — {end:%Y-%m-%d} 07:00 KST','',f'Window: {start.isoformat()} to {end.isoformat()}','Research only. No orders. Origin causality is uncalibrated.','',f'Shocks: {len(chains)}; state events: {len(states)}; matured outcomes: {len(evaluations)}','', 'On-chain → Global → Derivatives → Korea → VPD → Price','']
    for c in chains[-20:]:
        related=[s for s in states if s['shock_id']==c['shock_id']]
        lines += [f"## {c['asset']} {c['direction']} {c['horizon']} / {c['shock_id'][:10]}",f"- On-chain candidates: {len(c['onchain_candidates'])} (association, not proof of origin)",f"- Global origin candidate: {c['origin_venue']}; evidence score: {c['origin_evidence_score']:.2f}; calibrated confidence: unavailable",f"- Derivatives snapshots: {len(c['derivatives_context'])}",f"- Korea / propagation: {' → '.join(s['state'] for s in related)}",f"- VPD as-of: {json.dumps(c['vpd_join'],ensure_ascii=False)}",f"- Price evaluation: {c['entry_status']}",'']
    lines += ['## Matured outcome comparison','','Overlapping cohorts; gross signed returns without fees. BUY and SELL are separated. VPD_ONLY uses one BUY baseline per snapshot and common asset. Windows are grouped by evaluation completion time.','','| Cohort | Direction | Horizon | Valid / Missing | Mean return | Win rate | Mean MFE | Mean MAE |','|---|---|---:|---:|---:|---:|---:|---:|']
    for cohort in COHORTS:
        for direction in ('BUY','SELL'):
            for horizon in (10,30,60,300,1800,3600):
                xs=[x for x in evaluations if x['cohort']==cohort and x['direction']==direction and x['horizon_sec']==horizon]
                valid=[x for x in xs if x['status']=='COMPLETE']; n=len(valid)
                values=[f'{mean(x[k] for x in valid)*100:.3f}%' if n else 'N/A' for k in ('forward_return','mfe','mae')]
                win=f"{sum(x['forward_return']>0 for x in valid)/n:.1%}" if n else 'N/A'
                lines.append(f'| {cohort} | {direction} | {horizon}s | {n} / {len(xs)-n} | {values[0]} | {win} | {values[1]} | {values[2]} |')
    lines+=['','## Propagation observations','',f'Observed follower samples: {len(prop)}. Missing feeds are excluded from probability denominators.','']
    for p in prop[-20:]:lines.append(f"- {p['asset']} {p['origin_venue']} → {p['follower_venue']} {p['direction']}: P={p['reception_probability']:.3f}, CI95={p['reception_probability_ci95']}, n={p['sample_count']}, lag={p['lag_ms']} ms, sensitivity={p['sensitivity']}, usable lead={p['usable_lead_time_ms']} ms")
    root=Path(output_dir or storage.root/'reports');root.mkdir(parents=True,exist_ok=True)
    path=root/f'magi1_daily_{end:%Y%m%d}.md'; tmp=path.with_suffix('.tmp');tmp.write_text('\n'.join(lines)+'\n',encoding='utf-8');tmp.replace(path)
    return path
