"""Causal features from a separate FAST tape; no WAVE schema/state mutation."""
from collections import defaultdict,deque
import math
import statistics


def finite(x):
    x=float(x)
    if not math.isfinite(x):raise ValueError('NON_FINITE')
    return x


def levels(rows,reverse=False):
    pairs=[(finite(p),finite(q)) for p,q in rows]
    if not pairs or any(p<=0 or q<=0 for p,q in pairs):raise ValueError('BAD_DEPTH')
    return sorted(pairs,reverse=reverse)


class Features:
    def __init__(self):
        self.bars=defaultdict(lambda:deque(maxlen=180));self.books={};self.ofi=defaultdict(deque)
        self.last_id={};self.last_emit={};self.quality=defaultdict(int)
    def reset(self,venue):
        for mapping in (self.bars,self.books,self.ofi,self.last_id,self.last_emit):
            for key in list(mapping):
                if key[0]==venue:del mapping[key]
        self.quality['feed_gaps']+=1
    def observe(self,r):
        key=(r['venue'],r.get('symbol'))
        if r['kind']=='gap':self.reset(r['venue']);return None
        ts=int(r['received_ts_ms'])
        if r['kind']=='book':
            bids=levels(r['bids'],True);asks=levels(r['asks'])
            if bids[0][0]>asks[0][0]:raise ValueError('CROSSED_BOOK')
            old=self.books.get(key)
            if old and ts<old['received_ts_ms']:raise ValueError('OUT_OF_ORDER_BOOK')
            book={**r,'bids':bids,'asks':asks};self.books[key]=book
            if old and ts-old['received_ts_ms']<=2000:
                bp,bq=bids[0];ap,aq=asks[0];obp,obq=old['bids'][0];oap,oaq=old['asks'][0]
                flow=(bq if bp>=obp else 0)-(obq if bp<=obp else 0)-(aq if ap<=oap else 0)+(oaq if ap>=oap else 0)
                self.ofi[key].append((ts,flow))
            while self.ofi[key] and self.ofi[key][0][0]<ts-10000:self.ofi[key].popleft()
            return None
        if r['kind']!='trade':return None
        price,qty=finite(r['price']),finite(r['qty'])
        if price<=0 or qty<=0 or r['side'] not in ('BUY','SELL'):raise ValueError('INVALID_TRADE')
        ident=int(r['trade_id'])
        if ident<=self.last_id.get(key,-1):self.quality['duplicate_or_reordered_trade']+=1;return None
        bars=self.bars[key];sec=ts//1000
        if bars and sec<bars[-1]['sec']:raise ValueError('OUT_OF_ORDER_TRADE')
        if bars and sec-bars[-1]['sec']>5:
            bars.clear();self.ofi[key].clear();self.quality['inactive_or_missing_trade_gap']+=1
        self.last_id[key]=ident
        if not bars or bars[-1]['sec']!=sec:bars.append({'sec':sec,'price':price,'notional':0.,'buy':0.,'count':0})
        b=bars[-1];b['price']=price;b['notional']+=price*qty;b['count']+=1
        if r['side']=='BUY':b['buy']+=price*qty
        if self.last_emit.get(key)==sec:return None
        self.last_emit[key]=sec
        recent=[x for x in bars if sec-10<x['sec']<=sec]
        baseline=[x for x in bars if sec-70<x['sec']<=sec-10]
        if bars[0]['sec']>sec-70 or len(baseline)<45 or len(recent)<8:return None
        def ret(seconds):
            prior=[x for x in bars if x['sec']<=sec-seconds]
            if not prior or sec-seconds-prior[-1]['sec']>2:return None
            return (price/prior[-1]['price']-1)*10000
        r10=ret(10)
        if r10 is None:return None
        base_volume=sum(x['notional'] for x in baseline)/60*10
        volume=sum(x['notional'] for x in recent)
        if base_volume<=0 or volume<=0:return None
        deltas=[(b['price']/a['price']-1)*10000 for a,b in zip(baseline,baseline[1:]) if b['sec']-a['sec']==1]
        vol=statistics.pstdev(deltas)*math.sqrt(10) if len(deltas)>=30 else None
        book=self.books.get(key);spread=imbalance=flow=None
        if book and 0<=ts-book['received_ts_ms']<=1000:
            bp,bq=book['bids'][0];ap,aq=book['asks'][0]
            spread=(ap-bp)/((ap+bp)/2)*10000;imbalance=(bq-aq)/(bq+aq)
            flow=sum(value for t,value in self.ofi[key] if ts-10000<=t<=ts)/((bq+aq)/2)
        return {k:r[k] for k in ('venue','symbol','asset','quote')}|{
            'received_ts_ms':ts,'version':'fast-features-v1','return_10s_bps':r10,'return_30s_bps':ret(30),
            'return_60s_bps':ret(60),'volume_ratio':volume/base_volume,'buy_notional_ratio':sum(x['buy'] for x in recent)/volume,
            'trade_count_10s':sum(x['count'] for x in recent),'notional_10s':volume,'baseline_volatility_10s_bps':vol,
            'spread_bps':spread,'queue_imbalance':imbalance,'ofi_depth_normalized':flow,'baseline_active_seconds':len(baseline)}
