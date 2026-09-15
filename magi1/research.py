"""Runtime wiring for formation, point-in-time cohorts, propagation and outcomes."""
import uuid
from collections import defaultdict,deque
from dataclasses import asdict
from .features import FeatureEngine
from .formation import FormationEngine,VENUES
from .propagation import PropagationStats
from .vpd_join import VPDPointInTimeJoin
from .evaluation import evaluate
from .schema import TradeEvent,BookEvent
from .config import FORWARD_WINDOWS_SEC

class ResearchEngine:
    def __init__(self,storage,assets,now):
        self.storage=storage; self.assets=assets; self.features=FeatureEngine()
        self.formation=FormationEngine(self.emit); self.stats=PropagationStats()
        self.vpd=VPDPointInTimeJoin(storage.query('vpd_snapshot',now-4*86400000,now+1))
        self.pending=storage.restore('pending',{}); self.prices=defaultdict(lambda:deque(maxlen=3701))
        self.features_latest={}; self.feed_latest={}; self.first_feed={}; self.last_trade_ids={}; self.onchain=deque(maxlen=1000); self.derivatives=deque(maxlen=1000)
        self.completed=set(storage.restore('completed',[])); self.vpd_seen=set(storage.restore('vpd_seen',[])); self.baselines={}; self.vpd_candidates={}
        for row in storage.query('propagation',now-30*86400000,now+1): self.stats.add(row)
        for k,rows in storage.restore('prices',{}).items(): self.prices[tuple(k.split('|'))].extend(rows)
        # Open old episodes are not reused without fresh feature warm-up.
        for row in self.pending.values(): row['restarted']=True

    def ingest(self,e):
        self.storage.append_raw('trade' if isinstance(e,TradeEvent) else 'book',e)
        if isinstance(e,TradeEvent):
            key=(e.venue,e.base)
            if e.trade_id and e.trade_id==self.last_trade_ids.get(key):return
            self.last_trade_ids[key]=e.trade_id
            self.feed_latest[key]=e.received_ts_ms; self.first_feed.setdefault(key,e.received_ts_ms)
            q=self.prices[key]; value=[e.received_ts_ms,e.price]
            if q and q[-1][0]//1000==e.received_ts_ms//1000:q[-1]=value
            else:q.append(value)
            if e.venue=='upbit': self.vpd_entry(e)
        for f in self.features.ingest(e):
            self.features_latest[(f.venue,f.asset,f.horizon)]=f
            self.formation.ingest(f)

    def add_vpd(self,row):
        if any(x['ts_ms']==row['ts_ms'] for x in self.vpd.rows):return
        self.storage.append('vpd_snapshot',row); self.vpd.add(row)
        for asset in self.assets:
            x=row['assets'].get(asset,{})
            if x.get('vpd',0)>=75:
                key=f"{row['ts_ms']}:{asset}"
                if key not in self.vpd_seen:self.vpd_candidates[asset]=(key,row)

    def vpd_entry(self,e):
        candidate=self.vpd_candidates.get(e.base)
        if not candidate:return
        key,row=candidate
        # A VPD observation gets one forward-only baseline per snapshot/asset.
        if e.received_ts_ms<row['available_ms'] or e.received_ts_ms-row['ts_ms']>30*3600000:return
        self.vpd_seen.add(key); del self.vpd_candidates[e.base]
        join=self.vpd.join(e.received_ts_ms,e.base)
        self.entry(uuid.uuid4().hex,e.base,'BUY',e.received_ts_ms,e.price,['VPD_ONLY'],join,'VPD')

    def entry(self,shock_id,asset,direction,ts,price,cohorts,join,horizon):
        row={'shock_id':shock_id,'asset':asset,'direction':direction,'event_ts_ms':ts,'entry_price':price,'cohorts':cohorts,'vpd':join,'horizon':horizon,'done':[],'max_sample_gap_ms':5000}
        self.pending[shock_id]=row; self.storage.append('cohort_entry',row)

    def emit(self,kind,row):
        if kind=='propagation_observation':self.measure(row);return
        self.storage.append(kind,row)
        if row['state']!='FORMATION': return
        ts=row['event_ts_ms']; asset=row['asset']; prices=self.prices.get(('upbit',asset),[])
        price=prices[-1][1] if prices and ts-prices[-1][0]<=5000 else None
        f=self.features_latest.get(('upbit',asset,row['horizon']))
        reaction=f.return_bps if f and ts-f.ts_ms<=5000 else None
        join=self.vpd.join(ts,asset,reaction)
        cohorts=['FLOW_ONLY']
        # Existing VPD is a bullish scanner; SELL flows remain Flow-only observations.
        if row['direction']=='BUY' and join['vpd'] is not None and join['vpd']>=75:
            cohorts.append('FLOW_VPD')
            if join['price_non_reaction'] is True:cohorts.append('FLOW_VPD_NON_REACTION')
        chain={**row,'vpd_join':join,'entry_status':'READY' if price else 'MISSING_UPBIT_PRICE','cohorts':cohorts,'onchain_candidates':[x for x in self.onchain if x['asset']==asset and 0<=ts-x['received_ts_ms']<=3600000],'derivatives_context':[x for x in self.derivatives if x['asset']==asset and 0<=ts-x['received_ts_ms']<=60000],'reconstruction_order':['On-chain','Global','Derivatives','Korea','VPD','Price'],'causality':'temporal association only'}
        self.storage.append('shock_chain',chain)
        if price:self.entry(row['shock_id'],asset,row['direction'],ts,price,cohorts,join,row['horizon'])

    def measure(self,a):
        ts=a['origin_ts_ms']; origin=a['origin_venue']; asset=a['asset']
        for follower in sorted(VENUES-{origin}):
            x=a['venues'].get(follower); received=bool(x and 0<=x['ts_ms']-ts<=30000)
            # Do not count disconnected feeds as negative receptions.
            if self.first_feed.get((follower,asset),ts+1)>ts or a['event_ts_ms']-self.feed_latest.get((follower,asset),0)>5000:
                self.storage.append('propagation_missing',{'event_ts_ms':a['event_ts_ms'],'shock_id':a['shock_id'],'asset':asset,'follower':follower,'reason':'insufficient feed coverage'});continue
            lag=x['ts_ms']-ts if received else None
            move=a['origin_move_bps']
            row={'event_ts_ms':a['event_ts_ms'],'shock_id':a['shock_id'],'asset':asset,'direction':a['direction'],'horizon':a['horizon'],'origin_venue':origin,'follower_venue':follower,'received':received,'lag_ms':lag,'sensitivity':x['move_bps']/move if received and abs(move)>=1 else None,'usable_lead_time_ms':max(0,lag-1000) if lag is not None else None,'decision_budget_ms':1000,'timing_basis':'local_receive_detection; clock uncertainty not removed'}
            self.storage.append('propagation',self.stats.add(row))

    def tick(self,ts):
        self.formation.tick(ts)
        for shock_id,row in list(self.pending.items()):
            due=[h for h in FORWARD_WINDOWS_SEC if h not in row['done'] and ts>=row['event_ts_ms']+h*1000]
            if not due:continue
            series=list(self.prices.get(('upbit',row['asset']),[]))
            series.append((row['event_ts_ms'],row['entry_price']))
            for cohort in row['cohorts']:
                for result in evaluate(shock_id,cohort,row['direction'],row['entry_price'],series,row['event_ts_ms'],ts):
                    if result['horizon_sec'] in due:
                        key=f"{shock_id}:{cohort}:{result['horizon_sec']}"
                        if key not in self.completed:
                            self.storage.append('evaluation',{**result,'asset':row['asset'],'direction':row['direction'],'formation_horizon':row['horizon'],'restarted':row.get('restarted',False)}); self.completed.add(key)
            row['done'].extend(due)
            if len(row['done'])==len(FORWARD_WINDOWS_SEC):del self.pending[shock_id]

    def checkpoint(self):
        self.storage.checkpoint('pending',self.pending)
        self.storage.checkpoint('prices',{'|'.join(k):list(v) for k,v in self.prices.items()})
        self.storage.checkpoint('completed',sorted(self.completed)[-20000:])
        self.storage.checkpoint('vpd_seen',sorted(self.vpd_seen)[-2000:])
