"""Independent 3m KRW paper account with pre-funded venue wallets.

Spot+linear-perp pairs and directional shorts share ONE capital pool.
Pair fills are next-observation depth simulations, not executable arbitrage claims.
"""
from collections import defaultdict, deque
from decimal import Decimal, ROUND_DOWN
import json
import copy
import math
from pathlib import Path
import sqlite3
import statistics
from magi1.fast_paper import walk
from .market import fresh, key, ASSETS, SLIP
from .metrics import Metrics, INITIAL, DAY, VERSION


def fill(q,side,qty):
    done,value,left=walk(q['asks' if side=='BUY' else 'bids'],quantity=qty)
    if left>1e-8 or done<=0:raise ValueError('INSUFFICIENT_DEPTH')
    return value/done*(1+SLIP if side=='BUY' else 1-SLIP)


def quantize(qty,steps):
    # All supported venue steps are decimal powers; maximum is a common step.
    step=Decimal(str(max(steps)))
    return float((Decimal(str(qty))/step).to_integral_value(rounding=ROUND_DOWN)*step)


class Derivatives:
    def __init__(self,path,stamp):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts INTEGER,kind TEXT,payload TEXT);
          CREATE TABLE IF NOT EXISTS quotes(ts INTEGER,instrument TEXT,payload TEXT,PRIMARY KEY(ts,instrument));
        ''')
        row=self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        self.s=json.loads(row[0]) if row else dict(version=VERSION,started_ms=stamp,initial=INITIAL,
            wallets={},unallocated=INITIAL,positions={},pending={},funding_rates={},updated_ms=stamp,
            entry_days={},equity=INITIAL,valid=True)
        if self.s['version']!=VERSION:raise ValueError('COHORT_MISMATCH')
        self.metrics=Metrics(self.db,self.s['started_ms']);self.history=defaultdict(lambda:deque(maxlen=2160))
        self.audit={};self.last_snapshot=0;self.history_ts={}
        if row:
            self.s['pending']={}
            self.event(stamp,'RESTART',{'positions':len(self.s['positions']),'entry_warmup':True})
        self.save(stamp)

    def event(self,ts,kind,payload):
        self.db.execute('INSERT INTO events(ts,kind,payload) VALUES(?,?,?)',(ts,kind,json.dumps(payload,allow_nan=False)))

    def save(self,ts):
        if any(w['cash']<-.000001 for w in self.s['wallets'].values()):raise ValueError('NEGATIVE_WALLET')
        self.s['updated_ms']=ts
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',(json.dumps(self.s,allow_nan=False),));self.db.commit()

    def initialize_wallets(self,fx,ts):
        if self.s['wallets'] or not all(x in fx for x in ('KRW','USD','USDT')):return
        for venue in ('upbit','bithumb','binance','kraken'):
            for kind in (('spot','perp') if venue in ('binance','kraken') else ('spot',)):
                currency='KRW' if venue in ('upbit','bithumb') else 'USDT' if venue=='binance' else 'USD'
                self.s['wallets'][venue+':'+kind]=dict(currency=currency,cash=500000/fx[currency]['ask'],debt=0.,initial_krw=500000)
        self.s['unallocated']=0.
        self.event(ts,'VIRTUAL_PREFUNDING',dict(wallets=self.s['wallets'],fx=fx,
                   assumption='public FX bid/ask conversion; no transfers or borrowing'))

    def valid_quote(self,k,books,fx,ts):
        q=books.get(k)
        return bool(fresh(q,ts) and ts-q.get('meta_ts',0)<7_200_000 and q['quote'] in fx)

    def available(self,wallet):
        cash=self.s['wallets'].get(wallet,{}).get('cash',0.)
        return cash-sum(p['reserves'].get(wallet,0) for p in self.s['pending'].values())

    def funding(self,p,feeds,ts):
        """Apply actual published funding only, idempotently by a persisted cursor."""
        leg=p['legs'][-1];k=leg['instrument'];d=feeds.get(k)
        if not d or not 0<=ts-d['ts']<180000:return False
        start=p.get('funding_cursor',p['entry_ms'])
        if k.startswith('binance:'):
            if p.get('next_funding') is not None and ts>=p['next_funding']:
                if not any(e['ts']>=p['next_funding'] for e in d['events']):return False
            for e in d['events']:
                if start<e['ts']<=ts:
                    amount=p['qty']*e['mark']*e['rate'];leg['funding']+=amount
                    self.event(ts,'FUNDING',dict(position=p['id'],settled_ms=e['ts'],amount=amount,currency=leg['quote']))
            p['next_funding']=d['next']
        else:
            rates=self.s['funding_rates'].setdefault(k,{})
            rates.update(d['rates'])
            cursor=start;amount=0.
            while cursor<ts:
                hour=cursor//3_600_000*3_600_000
                if str(hour) not in rates:return False
                end=min(ts,hour+3_600_000)
                amount+=p['qty']*rates[str(hour)]*(end-cursor)/3_600_000;cursor=end
            leg['funding']+=amount
        p['funding_cursor']=ts
        return True

    def leg_value(self,leg,qty,q):
        price=fill(q,'SELL' if leg['kind']=='spot' else 'BUY',qty)
        if leg['kind']=='spot':return qty*price*(1-q['fee'])
        return leg['margin']+qty*(leg['entry']-price)+leg['funding']-qty*price*q['fee']

    def value(self,books,fx,ts):
        if not self.s['wallets']:return INITIAL,True
        total=self.s['unallocated'];valid=True
        for w in self.s['wallets'].values():
            if w['currency'] not in fx:valid=False;continue
            total+=(w['cash']-w.get('debt',0))*fx[w['currency']]['bid']
        for p in self.s['positions'].values():
            for l in p['legs']:
                if not self.valid_quote(l['instrument'],books,fx,ts):valid=False;continue
                try:total+=self.leg_value(l,p['qty'],books[l['instrument']])*fx[l['quote']]['bid']
                except ValueError:valid=False
            if not p.get('funding_valid',False):valid=False
        return (total if valid else None),valid

    def reserve(self,ident,kind,spot,perp,qty,fx,ts,features):
        if ident in self.s['pending'] or ident in self.s['positions'] or len(self.s['pending'])+len(self.s['positions'])>=10:return
        if self.s['entry_days'].get(perp['asset'])==ts//DAY:return
        if any(p['asset']==perp['asset'] for p in list(self.s['pending'].values())+list(self.s['positions'].values())):return
        if any(w.get('debt',0)>0 for w in self.s['wallets'].values()):return
        legs=[];reserves={}
        for q in ([spot] if spot else [])+[perp]:
            px=fill(q,'BUY' if q['kind']=='spot' else 'SELL',qty)
            # Perpetual margin 100% plus a 20% cash buffer, no netting across venues.
            capital=qty*px*(1+q['fee'] if q['kind']=='spot' else 1.2+q['fee'])
            wallet=q['venue']+':'+q['kind']
            reserves[wallet]=reserves.get(wallet,0)+capital
            if qty<q['min_qty'] or qty*px<q['min_notional']:return
            legs.append(dict(instrument=q['id'],quote=q['quote'],kind=q['kind'],wallet=wallet,reference=px))
        if sum(r*fx[self.s['wallets'][w]['currency']]['ask'] for w,r in reserves.items())>300000:return
        if any(self.available(w)<r for w,r in reserves.items()):return
        self.s['pending'][ident]=dict(id=ident,kind=kind,asset=perp['asset'],qty=qty,legs=legs,
            decision_ms=ts,reserves=reserves,features=features,expires=ts+30_000)
        self.event(ts,'SIGNAL',self.s['pending'][ident])

    def enter_pending(self,books,fx,feeds,ts,regime=None):
        for ident,p in list(self.s['pending'].items()):
            if ts>p['expires']:
                del self.s['pending'][ident];self.event(ts,'SKIP',dict(id=ident,reason='QUOTE_TIMEOUT'));continue
            if ts<=p['decision_ms']:continue
            if p['kind']=='SHORT' and (not regime or regime['state']!='DOWN' or ts-regime['ts']>=360000):
                del self.s['pending'][ident];continue
            if any(not self.valid_quote(l['instrument'],books,fx,ts) or books[l['instrument']]['ts']<=p['decision_ms'] for l in p['legs']):continue
            d=feeds.get(p['legs'][-1]['instrument'])
            if not d or ts-d['ts']>180000:continue
            if p['kind']=='BASIS' and d['rate']<0:
                del self.s['pending'][ident];continue
            if max(books[l['instrument']]['ts'] for l in p['legs'])-min(books[l['instrument']]['ts'] for l in p['legs'])>3000:continue
            legs=[];spent={};failed=False
            try:
                for l in p['legs']:
                    q=books[l['instrument']];price=fill(q,'BUY' if l['kind']=='spot' else 'SELL',p['qty'])
                    adverse=(price/l['reference']-1) if l['kind']=='spot' else (1-price/l['reference'])
                    if adverse>.003:raise ValueError('PRICE_CAP')
                    margin=0 if l['kind']=='spot' else p['qty']*price*1.2
                    cost=p['qty']*price*q['fee']+(p['qty']*price if l['kind']=='spot' else margin)
                    spent[l['wallet']]=spent.get(l['wallet'],0)+cost
                    legs.append(dict(l,entry=price,margin=margin,funding=0.,cost=cost,entry_fee=p['qty']*price*q['fee']))
                if sum(l['cost']*fx[l['quote']]['ask'] for l in legs)>300000:raise ValueError('SLOT_CAPITAL_LIMIT')
                if p['kind']=='BASIS':
                    spot_leg,perp_leg=legs
                    edge=perp_leg['entry']*fx[perp_leg['quote']]['bid']/(spot_leg['entry']*fx[spot_leg['quote']]['ask'])-1
                    fees=sum(2*books[l['instrument']]['fee'] for l in legs)
                    if edge-fees-2*SLIP-.002<.003:raise ValueError('PAIR_EDGE_LOST')
                if any(self.s['wallets'][w]['cash']-sum(x['reserves'].get(w,0) for i,x in self.s['pending'].items() if i!=ident)<c for w,c in spent.items()):raise ValueError('WALLET_LIMIT')
            except ValueError as e:
                failed=True;self.event(ts,'SKIP',dict(id=ident,reason=str(e)))
            del self.s['pending'][ident]
            if failed:continue
            for w,c in spent.items():self.s['wallets'][w]['cash']-=c
            cost_krw=sum(l['cost']*fx[l['quote']]['bid'] for l in legs)
            p.update(legs=legs,entry_ms=ts,capital=cost_krw,funding_cursor=ts,funding_valid=True,
                     next_funding=d['next'],peak_pnl=0.)
            self.s['positions'][ident]=p;self.s['entry_days'][p['asset']]=ts//DAY
            self.event(ts,'OPEN',p)

    def close_position(self,p,books,fx,ts,reason):
        values=[]
        try:
            for l in p['legs']:values.append(self.leg_value(l,p['qty'],books[l['instrument']]))
        except ValueError:return
        proceeds=sum(v*fx[l['quote']]['bid'] for v,l in zip(values,p['legs']))
        pnl=proceeds-p['capital']
        for value,l in zip(values,p['legs']):
            # Bankruptcy is explicit; never create negative wallet cash to finance losses.
            w=self.s['wallets'][l['wallet']]
            w['cash']+=max(0.,value)
            w['debt']=w.get('debt',0)+max(0.,-value)
        self.metrics.trade(p['id']+':'+str(p['entry_ms']),ts,p['kind'],pnl,
            dict(asset=p['asset'],reason=reason,capital=p['capital'],legs=p['legs'],entry_ms=p['entry_ms'],
                 insolvency=any(v<0 for v in values)))
        self.event(ts,'CLOSE',dict(id=p['id'],pnl=pnl,reason=reason))
        del self.s['positions'][p['id']]

    def exits(self,books,fx,feeds,ts):
        for p in list(self.s['positions'].values()):
            p['funding_valid']=self.funding(p,feeds,ts)
            if any(not self.valid_quote(l['instrument'],books,fx,ts) for l in p['legs']):continue
            try:proceeds=sum(self.leg_value(l,p['qty'],books[l['instrument']])*fx[l['quote']]['bid'] for l in p['legs'])
            except ValueError:continue
            ret=proceeds/p['capital']-1;p['peak_pnl']=max(p['peak_pnl'],ret)
            reason=None
            leg=p['legs'][-1];q=books[leg['instrument']];d=feeds.get(leg['instrument'])
            if d and 0<=ts-d['ts']<180000:
                margin=leg['margin']+p['qty']*(leg['entry']-d['mark'])+leg['funding']
                if margin<.75*p['qty']*d['mark']:reason='MARGIN_BUFFER'
            if p['kind']=='BASIS':
                if ret<=-.005:reason='PAIR_STOP'
                elif ret>=.005:reason='PAIR_PROFIT'
                elif ts-p['entry_ms']>=86_400_000:reason='PAIR_TIME_LIMIT'
                else:
                    spot=books[p['legs'][0]['instrument']]
                    spread=q['asks'][0][0]*fx[q['quote']]['ask']/(spot['bids'][0][0]*fx[spot['quote']]['bid'])-1
                    if spread<=p['features']['mean']:reason='BASIS_CONVERGENCE'
            else:
                move=1-q['asks'][0][0]/leg['entry']
                if move<=-.015:reason='SHORT_STOP'
                elif move>=.03:reason='SHORT_PROFIT'
                elif ts-p['entry_ms']>=14_400_000:reason='SHORT_TIME_LIMIT'
            if reason:
                # Persist exit intent first; fill from the next independent observation.
                if not p.get('exit'):p['exit']=dict(ts=ts,reason=reason)
            if (p.get('exit') and p['funding_valid'] and all(books[l['instrument']]['ts']>p['exit']['ts'] for l in p['legs'])
                and max(books[l['instrument']]['ts'] for l in p['legs'])-min(books[l['instrument']]['ts'] for l in p['legs'])<=3000):
                self.close_position(p,books,fx,ts,p['exit']['reason'])

    def scan(self,books,fx,feeds,regime,bear,ts):
        audit={};candidates=[]
        for a in ASSETS:
            for v in ('binance','kraken'):
                pk=key(v,'perp',a);perp=books.get(pk);d=feeds.get(pk)
                if not self.valid_quote(pk,books,fx,ts) or not d or ts-d['ts']>180000:continue
                for sv in ('upbit','bithumb','binance','kraken'):
                    sk=key(sv,'spot',a);spot=books.get(sk);ident=sk+'|'+pk
                    if not self.valid_quote(sk,books,fx,ts) or abs(spot['ts']-perp['ts'])>3000:continue
                    edge=perp['bids'][0][0]*fx[perp['quote']]['bid']/(spot['asks'][0][0]*fx[spot['quote']]['ask'])-1
                    history=self.history[ident]
                    if ts-self.history_ts.get(ident,ts)>60_000:history.clear()
                    self.history_ts[ident]=ts
                    mean=statistics.fmean(history) if history else edge
                    sd=statistics.pstdev(history) if len(history)>1 else 0.
                    net=edge-2*(spot['fee']+perp['fee'])-4*SLIP-.002
                    audit[ident]=dict(edge_bps=edge*10000,net_edge_bps=net*10000,samples=len(history),mean_bps=mean*10000)
                    if len(history)>=120 and net>=.003 and edge>mean+max(2*sd,.003) and d['rate']>=0:
                        candidates.append((net,ident,'BASIS',spot,perp,dict(mean=mean,edge=edge,net=net)))
                    history.append(edge)
                rows=bear.history.get('KRW-'+a,[]) if bear else []
                rows=[r for r in rows if r[0]+300000<=ts-2500]
                if (regime['state']=='DOWN' and 0<=ts-regime['ts']<360000 and len(rows)>=7 and
                    rows[-1][0]+300000>=self.s['started_ms'] and ts-(rows[-1][0]+300000)<600000 and
                    all(b[0]-a[0]==300000 for a,b in zip(rows[-7:],rows[-6:])) and
                    rows[-1][4]<min(r[3] for r in rows[-7:-1])):
                    candidates.append((0.,'SHORT|'+pk,'SHORT',None,perp,dict(signal_bar=rows[-1][0],regime=regime)))
        for _,ident,kind,spot,perp,f in sorted(candidates,key=lambda x:-x[0]):
            # 300k total collateral/spot cash per slot; aggregate initial capital remains 3m.
            unit=perp['bids'][0][0]*fx[perp['quote']]['ask']*1.202
            if spot:unit+=spot['asks'][0][0]*fx[spot['quote']]['ask']*(1+spot['fee']+.002)
            qty=quantize(300000/unit,[x['step'] for x in ([spot] if spot else [])+[perp]])
            try:self.reserve(ident,kind,spot,perp,qty,fx,ts,f)
            except ValueError:pass
        self.audit=audit

    def tick(self,books,fx,feeds,regime,bear,ts):
        before=copy.deepcopy(self.s);metrics_before=copy.deepcopy(self.metrics.s)
        try:self._tick(books,fx,feeds,regime,bear,ts)
        except Exception:
            self.db.rollback();self.s=before;self.metrics.s=metrics_before
            raise

    def _tick(self,books,fx,feeds,regime,bear,ts):
        self.initialize_wallets(fx,ts)
        if self.s['wallets']:
            self.exits(books,fx,feeds,ts)
            self.enter_pending(books,fx,feeds,ts,regime)
            self.scan(books,fx,feeds,regime,bear,ts)
        equity,valid=self.value(books,fx,ts)
        self.s.update(equity=equity,valid=valid,regime=regime)
        self.metrics.mark(ts,equity,valid)
        if ts-self.last_snapshot>=60_000:
            self.last_snapshot=ts
            for k,q in books.items():
                record={f:q[f] for f in ('venue','kind','asset','quote','ts','rtt','exchange_ts','fee')}
                record.update(bids=q['bids'][:3],asks=q['asks'][:3],funding=feeds.get(k,{}).get('rate'))
                self.db.execute('INSERT OR REPLACE INTO quotes VALUES(?,?,?)',(ts,k,json.dumps(record)))
            self.db.execute('DELETE FROM quotes WHERE ts<?',(ts-7*DAY,))
        self.save(ts)

    def report(self,ts):
        return dict(mode='FAST_DERIVATIVES_PAPER_ONLY',started_ms=self.s['started_ms'],initial=INITIAL,
            equity=self.s['equity'],valid=self.s['valid'],positions=len(self.s['positions']),pending=len(self.s['pending']),
            funded=bool(self.s['wallets']),mdd_pct=self.metrics.s['mdd'],pairs=len(self.audit),asof_ms=ts)
