"""Causal event replay, never an exchange order or production Shadow controller."""
import argparse
from collections import Counter,defaultdict
from dataclasses import dataclass,asdict
import gzip
import hashlib
import json
from pathlib import Path
import statistics
from magi1.fast_features import Features,finite,levels


@dataclass(frozen=True)
class Policy:
    version:str='fast-research-v1'
    min_return_bps:float=20
    min_volume_ratio:float=2
    min_buy_ratio:float=.6
    max_spread_bps:float=30
    volatility_multiple:float=3
    latency_ms:int=250
    quote_wait_ms:int=2000
    fee_bps:float=10
    slippage_bps:float=5
    take_profit_bps:float=40
    stop_loss_bps:float=30
    max_hold_ms:int=60000
    cooldown_ms:int=300000
    notional_krw:float=10000
    notional_usdt:float=10

    def __post_init__(self):
        values=asdict(self)
        for k,v in values.items():
            if k=='version':continue
            if isinstance(v,bool) or finite(v)<0:raise ValueError('INVALID_POLICY:'+k)
        if not 0<self.min_buy_ratio<=1 or not self.min_return_bps>0 or not self.min_volume_ratio>0:raise ValueError('INVALID_ENTRY_POLICY')
        if min(self.quote_wait_ms,self.max_hold_ms,self.notional_krw,self.notional_usdt,self.take_profit_bps,self.stop_loss_bps)<=0:raise ValueError('INVALID_EXECUTION_POLICY')
        if self.fee_bps>=10000 or self.slippage_bps>=10000:raise ValueError('INVALID_COST')
        if self.cooldown_ms<300000:raise ValueError('CLUSTER_COOLDOWN_AT_LEAST_300S')
        for key in ('latency_ms','quote_wait_ms','max_hold_ms','cooldown_ms'):
            if int(values[key])!=values[key]:raise ValueError('TIME_MUST_BE_INTEGER')

    @property
    def identity(self):return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()


def walk(rows,amount,by_qty=False):
    qty=cost=0.;left=amount
    for p,q in rows:
        take=min(q,left if by_qty else left/p);qty+=take;cost+=take*p
        left-=take if by_qty else take*p
        if left<=max(1e-12,amount*1e-10):return qty,cost
    return None # Do not treat partial visible liquidity as a fully executable order.


def candidate_reason(f,p):
    if f['return_10s_bps']<p.min_return_bps:return 'PRICE'
    vol=f['baseline_volatility_10s_bps']
    if vol is None:return 'BASELINE_MISSING'
    if f['return_10s_bps']<p.volatility_multiple*vol:return 'VOLATILITY_NORMALIZED_RETURN'
    if f['volume_ratio']<p.min_volume_ratio:return 'VOLUME'
    if f['buy_notional_ratio']<p.min_buy_ratio:return 'BUY_FLOW'
    if f['spread_bps'] is None:return 'STALE_OR_MISSING_BOOK'
    if f['spread_bps']>p.max_spread_bps:return 'SPREAD'
    return None


class Trial:
    def __init__(self,features,p,universe_id=None):
        self.p=p;self.key=(features['venue'],features['symbol']);self.state='ENTRY_PENDING'
        self.target=features['received_ts_ms']+p.latency_ms;self.last_quote=None
        ident=':'.join(map(str,(*self.key,features['received_ts_ms'],p.identity)))
        self.row={'id':hashlib.sha256(ident.encode()).hexdigest(),'features':features,'policy_id':p.identity,
                  'universe_id':universe_id,'mode':'OFFLINE_REPLAY','status':'PENDING','entry':None,
                  'forward':{},'policy_exit':None,'mfe_bps':None,'mae_bps':None}
    def exclude(self,reason):
        if self.state=='DONE':return
        for horizon in (10,30,60,300):self.row['forward'].setdefault(str(horizon),{'status':reason,'net_return_bps':None})
        if self.row['policy_exit'] is None:self.row['policy_exit']={'status':reason,'net_return_bps':None}
        self.row['status']=reason;self.state='DONE'
    def expire(self,now):
        if self.state=='ENTRY_PENDING' and now>self.target+self.p.quote_wait_ms:self.exclude('ENTRY_QUOTE_MISSING')
        elif self.state not in ('ENTRY_PENDING','DONE') and now-self.last_quote>self.p.quote_wait_ms:self.exclude('FORWARD_FEED_GAP')
    def exit_value(self,book):
        filled=walk(book['bids'],self.row['entry']['qty'],True)
        if not filled:return None
        _,gross=filled;proceeds=gross*(1-self.p.slippage_bps/10000)*(1-self.p.fee_bps/10000)
        return (proceeds/self.row['entry']['cost_quote']-1)*10000
    def book(self,book):
        ts=book['received_ts_ms'];self.expire(ts)
        if self.state=='DONE':return
        if self.state=='ENTRY_PENDING':
            if ts<self.target:return
            quote=self.row['features']['quote']
            notional={'KRW':self.p.notional_krw,'USDT':self.p.notional_usdt}.get(quote)
            if notional is None:self.exclude('UNSUPPORTED_QUOTE');return
            filled=walk(book['asks'],notional)
            if not filled:self.exclude('ENTRY_INSUFFICIENT_DEPTH');return
            qty,gross=filled
            cost=gross*(1+self.p.slippage_bps/10000)*(1+self.p.fee_bps/10000)
            self.row['entry']={'received_ts_ms':ts,'qty':qty,'cost_quote':cost,'quote':quote,
                               'gross_quote':gross,'actual_delay_ms':ts-self.row['features']['received_ts_ms']}
            self.state='OPEN';self.last_quote=ts;return
        self.last_quote=ts
        value=self.exit_value(book);elapsed=ts-self.row['entry']['received_ts_ms']
        if value is not None:
            self.row['mfe_bps']=value if self.row['mfe_bps'] is None else max(value,self.row['mfe_bps'])
            self.row['mae_bps']=value if self.row['mae_bps'] is None else min(value,self.row['mae_bps'])
        for horizon in (10,30,60,300):
            if str(horizon) in self.row['forward'] or elapsed<horizon*1000:continue
            valid=value is not None and elapsed-horizon*1000<=self.p.quote_wait_ms
            self.row['forward'][str(horizon)]={'status':'COMPLETE' if valid else 'EXIT_UNOBSERVABLE',
                'net_return_bps':value if valid else None,'received_ts_ms':ts,'horizon_from':'ENTRY'}
        if self.state=='EXIT_PENDING' and ts>=self.target:
            valid=value is not None and ts-self.target<=self.p.quote_wait_ms
            self.row['policy_exit']={'status':'COMPLETE' if valid else 'EXIT_UNOBSERVABLE',
                'net_return_bps':value if valid else None,'received_ts_ms':ts,'reason':self.exit_reason}
            self.state='OBSERVE_FORWARD'
        if self.state=='OPEN':
            reason=('TAKE_PROFIT' if value is not None and value>=self.p.take_profit_bps else
                    'STOP_LOSS' if value is not None and value<=-self.p.stop_loss_bps else
                    'TIME_LIMIT' if elapsed>=self.p.max_hold_ms else None)
            if reason:self.state='EXIT_PENDING';self.exit_reason=reason;self.target=ts+self.p.latency_ms
        if len(self.row['forward'])==4 and self.row['policy_exit'] is not None:
            self.state='DONE';self.row['status']='OBSERVED'


class Lab:
    def __init__(self,policy=None):
        self.p=policy or Policy();self.features=Features();self.active={};self.trials=[]
        self.cooldown={};self.counts=Counter();self.universes={};self.last_ts=-1;self.last_expire=-1
        self.members={};self.book_sequences={};self.input_records=0
    def feed(self,r):
        ts=int(r['received_ts_ms'])
        if ts<self.last_ts:raise ValueError('TAPE_NOT_IN_RECEIPT_ORDER')
        self.last_ts=ts;self.input_records+=1
        if ts//1000!=self.last_expire:
            for key,t in list(self.active.items()):
                t.expire(ts)
                if t.state=='DONE':del self.active[key]
            self.last_expire=ts//1000
        if r['kind']=='universe':
            venue=r['venue'];self.universes[venue]=r
            self.members[venue]={m['symbol'] for m in r['markets']};return
        if r['kind'] in ('capture_start','capture_end','connection'):return
        if r['kind']=='gap':
            self.features.reset(r['venue'])
            for key,t in list(self.active.items()):
                if key[0]==r['venue']:t.exclude('FEED_GAP');del self.active[key]
            for key in list(self.book_sequences):
                if key[0]==r['venue']:del self.book_sequences[key]
            return
        key=(r['venue'],r['symbol'])
        if r['symbol'] not in self.members.get(r['venue'],set()):raise ValueError('MISSING_POINT_IN_TIME_UNIVERSE')
        if r['kind']=='book' and 'sequence' in r:
            if r['sequence']<=self.book_sequences.get(key,-1):self.counts['REORDERED_BOOK']+=1;return
            self.book_sequences[key]=r['sequence']
        f=self.features.observe(r)
        if r['kind']=='book' and key in self.active:
            self.active[key].book(self.features.books[key])
            if self.active[key].state=='DONE':del self.active[key]
        if f is None:return
        self.counts['FEATURE_ROWS']+=1;reason=candidate_reason(f,self.p)
        if reason:self.counts['REJECT_'+reason]+=1;return
        self.counts['RAW_CANDIDATES']+=1
        if key in self.active or ts<self.cooldown.get(key,0):self.counts['CLUSTER_SUPPRESSED']+=1;return
        t=Trial(f,self.p,self.universes[r['venue']]['universe_id'])
        self.trials.append(t.row);self.active[key]=t;self.cooldown[key]=ts+self.p.cooldown_ms
    def finish(self):
        for t in self.active.values():t.exclude('END_OF_TAPE')
        self.active.clear()
        groups=defaultdict(list)
        for row in self.trials:
            f=row['features'];groups[(f['venue'],f['quote'])].append(row)
        summaries=[]
        for (venue,quote),rows in sorted(groups.items()):
            outcomes={}
            for label in ('10','30','60','300','policy'):
                xs=[r['policy_exit'] if label=='policy' else r['forward'][label] for r in rows]
                good=[x['net_return_bps'] for x in xs if x and x['status']=='COMPLETE']
                outcomes[label]={'trials':len(rows),'valid':len(good),'excluded':len(rows)-len(good),
                    'mean_net_bps':statistics.mean(good) if good else None,
                    'median_net_bps':statistics.median(good) if good else None,
                    'positive_fraction':sum(x>0 for x in good)/len(good) if good else None,
                    'status_counts':dict(Counter(x['status'] for x in xs if x))}
            summaries.append({'venue':venue,'quote':quote,'outcomes':outcomes})
        return {'schema':'fast-lab-report-v1','mode':'OFFLINE_REPLAY','profitability_verdict':'NOT_VALIDATED',
                'policy':asdict(self.p),'policy_id':self.p.identity,'counts':dict(self.counts),'quality':dict(self.features.quality),
                'input_records':self.input_records,'universes':list(self.universes.values()),'groups':summaries,
                'trials':self.trials,'limitations':['No real orders or portfolio capital constraint model.',
                'Displayed depth only; passive queue fills and market impact unknown. Costs are explicit assumptions.',
                'Forward horizons measured from observed simulated entry; MFE/MAE sampled at received quotes through 300s.',
                'Overlapping candidates suppressed per venue/symbol for >=300s. Cross-asset/day dependence still exists.',
                'Matched controls, walk-forward selection, clustered confidence intervals and external holdout remain required.']}


def read_tape(path):
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'rt',encoding='utf-8') as src:
        for line in src:
            if line.strip():yield json.loads(line)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True);p.add_argument('--output',required=True)
    p.add_argument('--policy',help='Frozen Policy JSON; omitted values use research defaults')
    args=p.parse_args();policy=Policy(**json.loads(Path(args.policy).read_text())) if args.policy else Policy()
    if Path(args.input).resolve()==Path(args.output).resolve():p.error('input and output must differ')
    if Path(args.output).exists():p.error('output already exists')
    lab=Lab(policy)
    for row in read_tape(args.input):lab.feed(row)
    result=lab.finish()
    with open(args.input,'rb') as src:
        h=hashlib.sha256()
        for chunk in iter(lambda:src.read(1024*1024),b''):h.update(chunk)
    result['tape_sha256']=h.hexdigest()
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    print(json.dumps({'mode':result['mode'],'trials':len(result['trials']),'verdict':'NOT_VALIDATED','output':str(output)}))
if __name__=='__main__':main()
