"""Runtime wiring for formation, point-in-time cohorts, propagation and outcomes."""
import uuid
from collections import defaultdict,deque,OrderedDict
from dataclasses import asdict
from .features import FeatureEngine
from .formation import FormationEngine,VENUES
from .propagation import PropagationStats
from .vpd_join import VPDPointInTimeJoin
from .schema import TradeEvent,BookEvent
from .derived_signals import DerivedSignalEngine
from .quality import QuoteCoverage, outcome, record_quality, VERSION, HORIZONS

class ResearchEngine:
    def __init__(self,storage,assets,now):
        self.storage=storage; self.assets=assets; self.features=FeatureEngine(); self.derived=DerivedSignalEngine(storage)
        self.formation=FormationEngine(self.emit); self.stats=PropagationStats()
        self.vpd=VPDPointInTimeJoin(storage.query('vpd_snapshot',now-4*86400000,now+1))
        self.quotes=QuoteCoverage(); self.trade_ids=defaultdict(OrderedDict); self.raw_books={}
        self.pending={}; self.prices=defaultdict(lambda:deque(maxlen=3701))
        for row in storage.restore('pending',{}).values():
            record_quality(storage,'RESTART_INTERRUPTED',now,row['asset'],version=row.get('evaluation_version','legacy'))
        self.features_latest={}; self.feed_latest={}; self.first_feed={}; self.last_trade_ids={}; self.onchain=deque(maxlen=1000); self.derivatives=deque(maxlen=1000)
        self.completed=set(storage.restore('completed',[])); self.vpd_seen=set(storage.restore('vpd_seen',[])); self.baselines={}; self.vpd_candidates={}
        for row in storage.query('propagation',now-30*86400000,now+1): self.stats.add(row)
        for k,rows in storage.restore('prices',{}).items(): self.prices[tuple(k.split('|'))].extend(rows)
        # Quote coverage cannot be carried across a process interruption.

    def ingest(self,e):
        key=(e.venue,e.base)
        if isinstance(e,BookEvent):
            if not self.quotes.ingest(e):
                record_quality(self.storage,'INVALID_BOOK',e.received_ts_ms,e.base,venue=e.venue);return
            signature=(tuple((x.price,x.quantity) for x in e.bids),tuple((x.price,x.quantity) for x in e.asks))
            old=self.raw_books.get(key)
            # Track changed books and one heartbeat per second in memory.
            if not old or old[0]!=signature or e.received_ts_ms-old[1]>=1000:
                # Raw book persistence is disabled; keep quote state for evaluation/features.
                self.raw_books[key]=(signature,e.received_ts_ms)
            else:record_quality(self.storage,'UNCHANGED_BOOK',e.received_ts_ms,e.base,venue=e.venue)
        if isinstance(e,TradeEvent):
            if e.trade_id:
                seen=self.trade_ids[key]
                if e.trade_id in seen:
                    record_quality(self.storage,'DUPLICATE_TRADE',e.received_ts_ms,e.base,venue=e.venue);return
                seen[e.trade_id]=None
                if len(seen)>4096:seen.popitem(last=False)
            self.storage.append_raw('trade',e)
            self.feed_latest[key]=e.received_ts_ms; self.first_feed.setdefault(key,e.received_ts_ms)
            q=self.prices[key]; value=[e.received_ts_ms,e.price]
            if q and q[-1][0]//1000==e.received_ts_ms//1000:q[-1]=value
            else:q.append(value)
            if e.venue=='upbit': self.vpd_entry(e)
        for f in self.features.ingest(e):
            self.features_latest[(f.venue,f.asset,f.horizon)]=f
            self.derived.on_feature(f)
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
        if not self.quotes.at('upbit',e.base,e.received_ts_ms):return
        self.vpd_seen.add(key); del self.vpd_candidates[e.base]
        join=self.vpd.join(e.received_ts_ms,e.base)
        self.entry(uuid.uuid4().hex,e.base,'BUY',e.received_ts_ms,e.price,['VPD_ONLY'],join,'VPD')

    def entry(self,shock_id,asset,direction,ts,price,cohorts,join,horizon):
        quote=self.quotes.at('upbit',asset,ts)
        if not quote:
            record_quality(self.storage,'ENTRY_QUOTE_UNAVAILABLE',ts,asset);return
        row={'evaluation_version':VERSION,'entry_bid':quote[1],'entry_ask':quote[2],'entry_quote_ts_ms':quote[0],'shock_id':shock_id,'asset':asset,'direction':direction,'event_ts_ms':ts,'entry_price':quote[2] if direction=='BUY' else quote[1],'cohorts':cohorts,'vpd':join,'horizon':horizon,'done':[],'max_sample_gap_ms':2500}
        self.pending[shock_id]=row; self.storage.append('cohort_entry',row)

    def emit(self,kind,row):
        if kind=='propagation_observation':self.measure(row);return
        self.storage.append(kind,row)
        if row['state']!='FORMATION': return
        ts=row['event_ts_ms']; asset=row['asset']; quote=self.quotes.at('upbit',asset,ts)
        price=(quote[2] if row['direction']=='BUY' else quote[1]) if quote else None
        f=self.features_latest.get(('upbit',asset,row['horizon']))
        reaction=f.return_bps if f and ts-f.ts_ms<=5000 else None
        join=self.vpd.join(ts,asset,reaction)
        cohorts=['FLOW_ONLY']
        # Existing VPD is a bullish scanner; SELL flows remain Flow-only observations.
        if row['direction']=='BUY' and join['vpd'] is not None and join['vpd']>=75:
            cohorts.append('FLOW_VPD')
            if join['price_non_reaction'] is True:cohorts.append('FLOW_VPD_NON_REACTION')
        chain={**row,'vpd_join':join,'entry_status':'READY' if price else 'MISSING_UPBIT_QUOTE','cohorts':cohorts,'onchain_candidates':[x for x in self.onchain if x['asset']==asset and 0<=ts-x['received_ts_ms']<=3600000],'derivatives_context':[x for x in self.derivatives if x['asset']==asset and 0<=ts-x['received_ts_ms']<=60000],'reconstruction_order':['On-chain','Global','Derivatives','Korea','VPD','Price'],'causality':'temporal association only'}
        self.storage.append('shock_chain',chain)
        if price:self.entry(row['shock_id'],asset,row['direction'],ts,price,cohorts,join,row['horizon'])
        else:record_quality(self.storage,'ENTRY_QUOTE_UNAVAILABLE',ts,asset)

    def measure(self,a):
        ts=a['origin_ts_ms']; origin=a['origin_venue']; asset=a['asset']
        for follower in sorted(VENUES-{origin}):
            x=a['venues'].get(follower); received=bool(x and 0<=x['ts_ms']-ts<=30000)
            # Do not count disconnected feeds as negative receptions.
            covered=self.quotes.covered(follower,asset,ts,a['event_ts_ms'])
            self.storage.append('reception_trial_v3',{
                'event_ts_ms':a['event_ts_ms'],'origin_ts_ms':ts,'shock_id':a['shock_id'],
                'asset':asset,'direction':a['direction'],'horizon':a['horizon'],
                'origin_venue':origin,'follower_venue':follower,
                'status':('RECEIVED' if received else 'NON_REACTION') if covered else 'UNOBSERVABLE',
                'reason':None if covered else 'QUOTE_GAP'})
            if not covered:
                record_quality(self.storage,'PROPAGATION_QUOTE_GAP',a['event_ts_ms'],asset,follower=follower,origin=origin);continue
            lag=x['ts_ms']-ts if received else None
            move=a['origin_move_bps']
            row={'coverage_version':VERSION,'event_ts_ms':a['event_ts_ms'],'shock_id':a['shock_id'],'asset':asset,'direction':a['direction'],'horizon':a['horizon'],'origin_venue':origin,'follower_venue':follower,'received':received,'lag_ms':lag,'sensitivity':x['move_bps']/move if received and abs(move)>=1 else None,'usable_lead_time_ms':max(0,lag-1000) if lag is not None else None,'decision_budget_ms':1000,'timing_basis':'local_receive_detection; clock uncertainty not removed'}
            self.storage.append('propagation',self.stats.add(row))

    def tick(self,ts):
        self.quotes.tick(ts)
        self.formation.tick(ts)
        for shock_id,row in list(self.pending.items()):
            due=[h for h in HORIZONS if h not in row['done'] and ts>=row['event_ts_ms']+h*1000]
            samples=list(self.quotes.samples[('upbit',row['asset'])])
            for horizon in due:
                # Quality denominator is independent of overlapping cohort memberships.
                first=outcome(row,row['cohorts'][0],horizon,samples)
                record_quality(self.storage,'EVALUATION_'+first['status'],ts,row['asset'],horizon,direction=row['direction'],version=VERSION)
                if first['status']=='COMPLETE':
                    for cohort in row['cohorts']:
                        result=outcome(row,cohort,horizon,samples)
                        self.storage.append('evaluation',{**result,'asset':row['asset'],'direction':row['direction'],'formation_horizon':row['horizon'],'restarted':False})
            row['done'].extend(due)
            if len(row['done'])==len(HORIZONS):del self.pending[shock_id]

    def checkpoint(self):
        self.storage.checkpoint('pending',self.pending)
        self.storage.checkpoint('prices',{'|'.join(k):list(v) for k,v in self.prices.items()})
        self.storage.checkpoint('completed',sorted(self.completed)[-20000:])
        self.storage.checkpoint('vpd_seen',sorted(self.vpd_seen)[-2000:])
