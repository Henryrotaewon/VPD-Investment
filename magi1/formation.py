from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import asdict
from typing import Callable

from .config import CONSENSUS_WINDOW_MS, FORMATION_RETURN_BPS, KOREA_RECEPTION_WINDOW_MS, NORMALIZATION_WINDOW_MS
from .features import FlowFeature
from .schema import FlowStateEvent

GLOBAL={"binance","bybit","kraken"}; KOREA={"upbit","bithumb","coinone"}


class FormationEngine:
    def __init__(self, emit: Callable[[str, object],None]):
        self.emit=emit; self.active: dict[tuple[str,str],dict]={}; self.last: dict[tuple[str,str],FlowFeature]={}

    def ingest(self, f: FlowFeature) -> None:
        threshold=FORMATION_RETURN_BPS*{"MICRO":1,"MESO":2,"MACRO":3}[f.horizon]
        direction="BUY" if f.return_bps>=threshold else "SELL" if f.return_bps<=-threshold else None
        self.last[(f.asset,f.venue)]=f
        if not direction: self._normalize(f); return
        key=(f.asset,direction,f.horizon); a=self.active.get(key)
        if not a:
            a=self.active[key]={"id":uuid.uuid4().hex,"start":f.ts_ms,"venues":{},"states":set()}
            self._state(a,f,direction,"FORMATION")
        if f.ts_ms-a["start"]>NORMALIZATION_WINDOW_MS: return
        a["venues"][f.venue]=f
        globals_={v for v in a["venues"] if v in GLOBAL}; korea={v for v in a["venues"] if v in KOREA}
        if len(globals_)>=2: self._state(a,f,direction,"GLOBAL_CONSENSUS")
        if globals_ and korea and f.ts_ms-a["start"]<=KOREA_RECEPTION_WINDOW_MS: self._state(a,f,direction,"KOREA_EARLY_RECEPTION")
        if len(globals_)>=2 and len(korea)>=2: self._state(a,f,direction,"PROPAGATION")

    def _state(self,a,f,direction,state):
        if state in a["states"]: return
        a["states"].add(state); venues=a["venues"]
        origin=min(venues.values(),key=lambda x:x.ts_ms).venue if venues else f.venue
        confidence=min(1.0,.4+.15*len(venues))
        self.emit("flow_state",FlowStateEvent(f.asset,direction,state,f.ts_ms,len(set(venues)&GLOBAL),len(set(venues)&KOREA),origin,confidence,min(1,abs(f.return_bps)/20),None,"magi1-v0.2",f.horizon))

    def _normalize(self,f):
        for key,a in list(self.active.items()):
            if key[0]==f.asset and key[2]==f.horizon and f.ts_ms-a["start"]>=NORMALIZATION_WINDOW_MS:
                self._state(a,f,key[1],"NORMALIZATION"); del self.active[key]
