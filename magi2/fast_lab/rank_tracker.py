"""FAST v2: sparse venue-local rankings and candidate-only repeated PAPER scalps.

Pure causal state machines. No network, credentials, real orders or Shadow state.
Feed one price snapshot per minute; call scan every 5/10/15/30 minutes.
Feed L1 quotes ONLY for the returned watchlist during its bounded watch window.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
import math


def positive(value):
    return isinstance(value,(int,float)) and math.isfinite(value) and value>0


@dataclass(frozen=True)
class RankPolicy:
    scan_minutes:int=15
    top_n:int=5
    watch_minutes:int=5
    min_rise_bps:float=100
    min_rank_jump:int=5
    baseline_tolerance_ms:int=90000
    max_current_age_ms:int=90000
    def __post_init__(self):
        if self.scan_minutes not in (5,10,15,30) or not 1<=self.top_n<=10 or not 5<=self.watch_minutes<=15:
            raise ValueError('INVALID_RANK_POLICY')
        if not positive(self.min_rise_bps) or self.min_rank_jump<1 or self.baseline_tolerance_ms<0 or self.max_current_age_ms<0:
            raise ValueError('INVALID_RANK_POLICY')


class RankTracker:
    def __init__(self,policy=None):
        self.p=policy or RankPolicy();self.history=defaultdict(deque)
        self.previous={};self.last_scan={};self.last_snapshot={}
    def snapshot(self,venue,ts,prices):
        if ts<=self.last_snapshot.get(venue,-1):raise ValueError('OUT_OF_ORDER_SNAPSHOT')
        self.last_snapshot[venue]=ts
        for symbol,price in prices.items():
            if not positive(price):continue
            rows=self.history[(venue,symbol)];rows.append((ts,float(price)))
            while rows and rows[0][0]<ts-1800000-self.p.baseline_tolerance_ms:rows.popleft()
        # Delisted / absent symbols cannot accumulate forever.
        for key in list(self.history):
            if key[0]==venue and self.history[key][-1][0]<ts-1800000-self.p.baseline_tolerance_ms:
                del self.history[key]
    def returns(self,venue,ts):
        result=[]
        for (v,symbol),rows in self.history.items():
            if v!=venue or ts-rows[-1][0]>self.p.max_current_age_ms:continue
            values={};price=rows[-1][1]
            for minutes in (5,10,15,30):
                target=ts-minutes*60000
                base=next(((t,p) for t,p in reversed(rows) if t<=target),None)
                values[str(minutes)]=(price/base[1]-1)*10000 if base and target-base[0]<=self.p.baseline_tolerance_ms else None
            if values[str(self.p.scan_minutes)] is not None:
                result.append({'venue':v,'symbol':symbol,'price':price,'returns_bps':values})
        return sorted(result,key=lambda r:(-r['returns_bps'][str(self.p.scan_minutes)],r['symbol']))
    def scan(self,venue,ts):
        last=self.last_scan.get(venue)
        if last is not None and ts-last<self.p.scan_minutes*60000:return None
        rows=self.returns(venue,ts)
        if not rows:return {'status':'WARMING_UP','venue':venue,'watchlist':[],'ranked':0}
        old=self.previous.get(venue,{})
        previous_fresh=last is not None and ts-last<=self.p.scan_minutes*60000+self.p.baseline_tolerance_ms
        if not previous_fresh:old={}
        watch=[]
        for rank,row in enumerate(rows,1):
            row['rank']=rank;prev=old.get(row['symbol']);row['previous_rank']=prev
            row['rank_jump']=prev-rank if prev is not None else None
            top=rank<=self.p.top_n
            jumped=prev is not None and prev-rank>=self.p.min_rank_jump
            if (top or jumped) and row['returns_bps'][str(self.p.scan_minutes)]>=self.p.min_rise_bps:
                row['reason']='TOP_RISER' if top else 'RANK_JUMP'
                watch.append(row)
        # Large rank jumps first, then strongest current short-window return.
        watch.sort(key=lambda r:(-(r['rank_jump'] or 0),r['rank']))
        self.previous[venue]={r['symbol']:r['rank'] for r in rows};self.last_scan[venue]=ts
        return {'status':'RANKED','venue':venue,'scan_ts_ms':ts,'ranking_minutes':self.p.scan_minutes,
                'watch_until_ms':ts+self.p.watch_minutes*60000,'ranked':len(rows),
                'watchlist':watch[:self.p.top_n],'top_risers':rows[:self.p.top_n],
                'policy':asdict(self.p),'mode':'PAPER_RESEARCH','profitability_verdict':'NOT_VALIDATED'}


@dataclass(frozen=True)
class ScalpPolicy:
    fee_bps:float=10
    slippage_bps:float=5
    max_spread_bps:float=15
    breakout_bps:float=5
    target_net_bps:float=15
    stop_net_bps:float=45
    reversal_bps:float=15
    lookback_ms:int=30000
    max_hold_ms:int=60000
    cooldown_ms:int=30000
    latency_ms:int=250
    max_quote_age_ms:int=1000
    max_gap_ms:int=2000
    max_trades_per_session:int=15
    loss_budget_bps:float=100
    def __post_init__(self):
        for key,value in asdict(self).items():
            if not positive(value):raise ValueError('INVALID_SCALP_POLICY_'+key)
        if self.max_trades_per_session>15:raise ValueError('MAX_15_TRADES')
        if self.stop_net_bps<=2*(self.fee_bps+self.slippage_bps):
            raise ValueError('STOP_MUST_EXCEED_ASSUMED_ROUND_TRIP_COST')


class PaperScalper:
    """One symbol per session. Full displayed-L1 fills only; no capital model.

Session caps are shared by all symbols using the same budget dictionary.
No target number of trades, no averaging down, no forced re-entry.
Invalid/missing quotes exclude an open experiment rather than inventing a fill.
"""
    def __init__(self,symbol,watch_until_ms,notional,policy=None,budget=None):
        if not positive(notional):raise ValueError('INVALID_NOTIONAL')
        self.symbol=symbol;self.until=watch_until_ms;self.notional=notional
        self.p=policy or ScalpPolicy();self.budget=budget if budget is not None else {'entries':0,'net_bps':0.}
        self.history=deque();self.last=None;self.state='WATCH';self.pending=None
        self.position=None;self.cooldown_until=0;self.trades=[];self.exclusions=[]
    def exclude(self,reason):
        self.exclusions.append(reason);self.position=None;self.pending=None;self.state='HALTED'
    def net(self,bid):
        return (bid*(1-self.p.slippage_bps/10000)*(1-self.p.fee_bps/10000)/self.position['unit_cost']-1)*10000
    def quote(self,ts,bid,ask,bid_size,ask_size,received_ms=None):
        p=self.p
        if self.last is not None and ts<=self.last:raise ValueError('OUT_OF_ORDER_QUOTE')
        old=self.last;self.last=ts
        if self.state=='HALTED':return
        valid=all(positive(v) for v in (bid,ask,bid_size,ask_size)) and bid<=ask
        valid=valid and (received_ms is None or 0<=ts-received_ms<=p.max_quote_age_ms)
        if not valid:
            if self.position or self.pending:self.exclude('INVALID_OR_STALE_QUOTE')
            self.history.clear();return
        if old is not None and ts-old>p.max_gap_ms:
            if self.position or self.pending:self.exclude('QUOTE_GAP');return
            self.history.clear()
        if self.position:
            value=self.net(bid)
            reason=('TARGET_NET' if value>=p.target_net_bps else 'STOP_NET' if value<=-p.stop_net_bps else
                    'BREAKOUT_FAILED' if bid<self.position['trigger_bid']*(1-p.reversal_bps/10000) else
                    'TIME_LIMIT' if ts-self.position['entry_ms']>=p.max_hold_ms else
                    'WATCH_ENDED' if ts>=self.until else None)
            if self.state=='EXIT_PENDING' and ts>=self.pending['due']:
                if ts-self.pending['due']>p.max_gap_ms or bid_size<self.position['qty']:
                    self.exclude('EXIT_UNOBSERVABLE');return
                trade={**self.position,'exit_ms':ts,'net_bps':value,'exit_reason':self.pending['reason']}
                self.trades.append(trade);self.budget['net_bps']+=value
                self.position=None;self.pending=None;self.state='WATCH';self.cooldown_until=ts+p.cooldown_ms
                self.history.clear()
            elif reason and self.state!='EXIT_PENDING':
                self.state='EXIT_PENDING';self.pending={'due':ts+p.latency_ms,'reason':reason}
            return
        if ts>=self.until or self.budget['entries']>=p.max_trades_per_session or self.budget['net_bps']<=-p.loss_budget_bps:
            self.state='HALTED';self.pending=None;return
        if self.state=='ENTRY_PENDING' and ts>=self.pending['due']:
            spread=(ask/bid-1)*10000
            if ts-self.pending['due']>p.max_gap_ms or spread>p.max_spread_bps or ask_size*ask<self.notional:
                self.state='WATCH';self.pending=None;self.history.clear();return
            self.position={'symbol':self.symbol,'entry_ms':ts,'qty':self.notional/ask,
                           'unit_cost':ask*(1+p.slippage_bps/10000)*(1+p.fee_bps/10000),
                           'trigger_bid':self.pending['trigger_bid']}
            self.budget['entries']+=1;self.state='OPEN';self.pending=None;return
        while len(self.history)>1 and self.history[1][0]<=ts-p.lookback_ms:self.history.popleft()
        ready=self.history and ts-self.history[0][0]>=p.lookback_ms
        high=max((price for _,price in self.history),default=bid)
        spread=(ask/bid-1)*10000
        if self.state=='WATCH' and ts>=self.cooldown_until and ready and bid>=high*(1+p.breakout_bps/10000) and spread<=p.max_spread_bps:
            self.state='ENTRY_PENDING';self.pending={'due':ts+p.latency_ms,'trigger_bid':bid}
        self.history.append((ts,bid))
    def finish(self):
        if self.position or self.pending:self.exclude('END_OF_OBSERVATION')
        else:self.state='HALTED'
        return {'symbol':self.symbol,'trades':self.trades,'exclusions':self.exclusions,
                'mode':'PAPER_RESEARCH','profitability_verdict':'NOT_VALIDATED','policy':asdict(self.p)}
