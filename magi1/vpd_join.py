"""Read-only scanner ingestion, gated by both source and local availability time."""
import csv
import io
from datetime import datetime

class VPDPointInTimeJoin:
    def __init__(self,snapshots=None): self.rows=list(snapshots or [])
    def add(self,row):
        if not any(x['ts_ms']==row['ts_ms'] for x in self.rows):
            self.rows.append(row); self.rows.sort(key=lambda x:x['ts_ms']); self.rows=self.rows[-100:]
    def join(self,ts_ms,asset,price_reaction_bps=None,max_age_ms=30*3600*1000):
        xs=[x for x in self.rows if x['ts_ms']<=ts_ms and x.get('available_ms',x['ts_ms'])<=ts_ms and ts_ms-x['ts_ms']<=max_age_ms]
        item=xs[-1]['assets'].get(asset,{}) if xs else {}
        return {'vpd':item.get('vpd'),'delta_vpd':item.get('delta_vpd'),'momentum':item.get('momentum'),'vpd_ts_ms':xs[-1]['ts_ms'] if xs else None,'price_non_reaction':abs(price_reaction_bps)<2 if price_reaction_bps is not None else None,'price_reaction_bps':price_reaction_bps}

def snapshot(payload,received,all_csv=None, commit_sha=None):
    ts=int(datetime.fromisoformat(payload['asof']).timestamp()*1000)
    assets={}
    for name in ('top10','qualified_rockets'):
        for x in payload.get(name,[]):
            asset=x.get('coin') or x.get('market','').removeprefix('KRW-')
            assets[asset]={'vpd':float(x['VPD']),'delta_vpd':x.get('VPDVelocity'),'momentum':x.get('momentum')}
    if all_csv:
        for x in csv.DictReader(io.StringIO(all_csv.lstrip('\ufeff'))):
            # CSV is used only when its row timestamp exactly matches JSON snapshot.
            stamp=x.get('asof') or x.get('asof_kst') or x.get('timestamp')
            if not commit_sha and stamp not in (payload['asof'],payload.get('asof_kst')): continue
            asset=x.get('coin') or x.get('market','').removeprefix('KRW-')
            try: assets[asset]={'vpd':float(x['VPD']),'delta_vpd':float(x.get('VPDVelocity') or 0),'momentum':x.get('momentum')}
            except (ValueError,KeyError): continue
    return {'ts_ms':ts,'available_ms':received,'event_ts_ms':received,'assets':assets,'commit_sha':commit_sha}
