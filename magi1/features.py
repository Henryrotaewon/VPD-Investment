"""Bounded one-second buckets; no full tick-history scan per incoming trade."""
from collections import defaultdict,deque
from dataclasses import dataclass
from .schema import BookEvent,TradeEvent

@dataclass(frozen=True)
class FlowFeature:
    venue:str
    asset:str
    ts_ms:int
    horizon:str
    return_bps:float
    signed_volume:float
    volume:float
    book_imbalance:float|None
    spread_bps:float|None
    volume_acceleration:float=0
    coverage_ms:int=0
    price:float=0

class FeatureEngine:
    WINDOWS={'MICRO':10,'MESO':60,'MACRO':600}
    def __init__(self):
        self.buckets=defaultdict(lambda:deque(maxlen=1201)); self.books={}; self.last_emit={}
    def ingest(self,e):
        key=(e.venue,e.base)
        if isinstance(e,BookEvent): self.books[key]=e; return []
        if not isinstance(e,TradeEvent): return []
        q=self.buckets[key]; sec=e.received_ts_ms//1000
        if q and sec<q[-1][0]: return []
        if not q or q[-1][0]!=sec: q.append([sec,e.price,e.price,0.,0.,e.received_ts_ms])
        b=q[-1]; b[2]=e.price; b[3]+=e.quantity; b[4]+=e.quantity*(1 if e.side=='BUY' else -1 if e.side=='SELL' else 0)
        if e.received_ts_ms-self.last_emit.get(key,-1000)<1000: return []
        self.last_emit[key]=e.received_ts_ms
        book=self.books.get(key); imb=spread=None
        if book and e.received_ts_ms-book.received_ts_ms<=5000 and book.mid:
            bv=sum(x.quantity for x in book.bids); av=sum(x.quantity for x in book.asks)
            imb=(bv-av)/(bv+av) if bv+av else None
            spread=(book.best_ask-book.best_bid)/book.mid*10000
        out=[]
        for name,window in self.WINDOWS.items():
            xs=[x for x in q if sec-window<=x[0]<=sec]
            prev=[x for x in q if sec-2*window<=x[0]<sec-window]
            coverage=e.received_ts_ms-xs[0][5]
            if coverage<window*1000*.8: continue
            vol=sum(x[3] for x in xs); signed=sum(x[4] for x in xs); pv=sum(x[3] for x in prev)
            out.append(FlowFeature(e.venue,e.base,e.received_ts_ms,name,(e.price/xs[0][1]-1)*10000,signed,vol,imb,spread,vol/pv if pv and len(prev)>=window*.8 else 0,coverage,e.price))
        return out
