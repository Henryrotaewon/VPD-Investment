from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .schema import BookEvent, TradeEvent


@dataclass(frozen=True)
class FlowFeature:
    venue: str; asset: str; ts_ms: int; horizon: str; return_bps: float
    signed_volume: float; volume: float; book_imbalance: float | None; spread_bps: float | None


class FeatureEngine:
    def __init__(self, retention_ms: int = 3_600_000):
        self.retention_ms = retention_ms
        self.trades: dict[tuple[str,str], deque[TradeEvent]] = defaultdict(deque)
        self.books: dict[tuple[str,str], BookEvent] = {}

    def ingest(self, event: Any) -> list[FlowFeature]:
        if isinstance(event, BookEvent): self.books[(event.venue,event.base)] = event; return []
        if not isinstance(event, TradeEvent): return []
        key=(event.venue,event.base); q=self.trades[key]; q.append(event)
        while q and q[0].received_ts_ms < event.received_ts_ms-self.retention_ms: q.popleft()
        result=[]
        for name,ms in (("MICRO",10_000),("MESO",600_000),("MACRO",3_600_000)):
            xs=[x for x in q if x.received_ts_ms >= event.received_ts_ms-ms]
            if len(xs)<2: continue
            vol=sum(x.quantity for x in xs); signed=sum(x.quantity*(1 if x.side=="BUY" else -1 if x.side=="SELL" else 0) for x in xs)
            b=self.books.get(key); imb=spread=None
            if b and b.best_bid and b.best_ask:
                bv=sum(x.quantity for x in b.bids); av=sum(x.quantity for x in b.asks); imb=(bv-av)/(bv+av) if bv+av else 0
                spread=(b.best_ask-b.best_bid)/b.mid*10_000 if b.mid else None
            result.append(FlowFeature(event.venue,event.base,event.received_ts_ms,name,(event.price/xs[0].price-1)*10_000,signed,vol,imb,spread))
        return result
