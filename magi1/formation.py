"""Ordered causal state machine; heuristic evidence is not a calibrated probability."""
import uuid
from dataclasses import asdict

GLOBAL={'binance','bybit','kraken'}
KOREA={'upbit','bithumb','coinone'}
VENUES=GLOBAL|KOREA

class FormationEngine:
    def __init__(self,emit):
        self.emit=emit; self.active={}; self.cooldown={}

    def ingest(self,f):
        threshold={'MICRO':4,'MESO':8,'MACRO':12}[f.horizon]
        imbalance=f.signed_volume/f.volume if f.volume else 0
        direction=None
        # Price acceleration OR independent flow pressure can form a candidate.
        for sign,side in ((1,'BUY'),(-1,'SELL')):
            pressure=sign*imbalance>=.6 and f.volume_acceleration>=2
            if pressure or (sign*f.return_bps>=threshold and sign*imbalance>=.2):
                direction=side; break
        if direction is None: return
        key=(f.asset,direction,f.horizon)
        if f.ts_ms<self.cooldown.get(key,0): return
        a=self.active.get(key)
        if a is None:
            a={'shock_id':uuid.uuid4().hex,'asset':f.asset,'direction':direction,'horizon':f.horizon,'event_ts_ms':f.ts_ms,'origin_venue':f.venue,'origin_move_bps':f.return_bps,'origin_confidence':None,'origin_evidence_score':sum((abs(imbalance)>=.6,f.volume_acceleration>=2,f.book_imbalance is not None and (1 if direction=='BUY' else -1)*f.book_imbalance>=.3))/3,'confidence_status':'uncalibrated_evidence_only','venues':{},'state':'FORMATION','stage':0,'origin_feature':asdict(f)}
            self.active[key]=a
            a['venues'][f.venue]={'ts_ms':f.ts_ms,'move_bps':f.return_bps}
            self.emit('flow_state',dict(a))
        a['venues'].setdefault(f.venue,{'ts_ms':f.ts_ms,'move_bps':f.return_bps})
        elapsed=f.ts_ms-a['event_ts_ms']
        g=[v for v,x in a['venues'].items() if v in GLOBAL and x['ts_ms']-a['event_ts_ms']<=10_000]
        k=[v for v,x in a['venues'].items() if v in KOREA and x['ts_ms']-a['event_ts_ms']<=30_000]
        if a['stage']==0 and len(g)>=2 and elapsed<=10_000:
            self.transition(a,'GLOBAL_CONSENSUS',f.ts_ms,1)
        if a['stage']==1 and k and elapsed<=30_000:
            self.transition(a,'KOREA_EARLY_RECEPTION',f.ts_ms,2)
        if a['stage']==2 and len(k)>=2 and elapsed<=30_000:
            self.transition(a,'PROPAGATION',f.ts_ms,3)

    def transition(self,a,state,ts,stage):
        a['state']=state; a['stage']=stage
        self.emit('flow_state',{**a,'event_ts_ms':ts,'origin_ts_ms':a['event_ts_ms'],'venues':{v:dict(x) for v,x in a['venues'].items()}})

    def tick(self,ts):
        for key,a in list(self.active.items()):
            if ts-a['event_ts_ms']>=30_000 and not a.get('measured'):
                a['measured']=True
                self.emit('propagation_observation',{**a,'event_ts_ms':ts,'origin_ts_ms':a['event_ts_ms']})
            if ts-a['event_ts_ms']>=300_000:
                self.transition(a,'NORMALIZATION',ts,4)
                del self.active[key]; self.cooldown[key]=ts+10_000
