"""Additive derived signals over existing MAGI1 observations.

Never mutates raw trade/book, FeatureEngine, formation, propagation or evaluation.
FAST is acceleration; WHALE is evidence from already-retained public on-chain candidates.
"""
from collections import defaultdict,deque
from dataclasses import dataclass,asdict
import math

@dataclass(frozen=True)
class DerivedSignal:
    event_ts_ms:int; asset:str; signal_type:str; direction:str; score:float
    confidence:float; evidence:dict; version:str="derived-v1"

class DerivedSignalEngine:
    def __init__(self,storage):
        self.storage=storage;self.hist=defaultdict(lambda:deque(maxlen=12));self.last={}
    def on_feature(self,f):
        if f.horizon!="MICRO":return
        k=(f.venue,f.asset);h=self.hist[k];h.append(f)
        if len(h)<3:return
        prev=list(h)[-2]
        accel=f.return_bps-prev.return_bps
        vol_accel=f.volume_acceleration
        imbalance=f.book_imbalance
        # Transparent research score, not calibrated probability.
        score=min(100,max(0,abs(accel)*2+max(0,vol_accel)*12+abs(imbalance)*18))
        direction="BUY" if accel>0 else "SELL"
        if score<55:return
        key=("FAST",f.asset,direction)
        if f.ts_ms-self.last.get(key,0)<10000:return
        self.last[key]=f.ts_ms
        row=DerivedSignal(f.ts_ms,f.asset,"FAST",direction,score,min(.99,score/100),
          {"venue":f.venue,"horizon":f.horizon,"return_bps":f.return_bps,"return_acceleration_bps":accel,
           "volume_acceleration":vol_accel,"book_imbalance":imbalance,"coverage_ms":f.coverage_ms,
           "confidence_semantics":"heuristic_score_not_calibrated_probability"})
        self.storage.append("derived_signal",asdict(row))
    def on_onchain(self,candidate):
        if candidate.category!="large_transfer":return
        amount=float(candidate.amount or 0)
        # Existing collector threshold is already large; size raises evidence strength.
        score=min(100,55+15*max(0,math.log10(max(amount,1)/100)))
        evidence={"source":candidate.source,"provider":candidate.provider,"amount":amount,"tx_hash":candidate.tx_hash,
          "classification":candidate.metadata.get("label","unclassified"),
          "causality":"large transfer observation only; wallet ownership/direction not established"}
        row=DerivedSignal(candidate.received_ts_ms,candidate.asset,"WHALE",candidate.direction,score,
          min(.99,score/100),evidence)
        self.storage.append("derived_signal",asdict(row))
