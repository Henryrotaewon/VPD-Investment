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
    confidence:float; evidence:dict; version:str="derived-v2"

class DerivedSignalEngine:
    def __init__(self,storage):
        self.storage=storage;self.hist=defaultdict(lambda:deque(maxlen=12));self.last={}
    def on_feature(self,f):
        if f.horizon!="MICRO":return
        if f.coverage_ms < 8000:return
        values=(f.return_bps,f.volume_acceleration)
        if any(x is None or not math.isfinite(x) for x in values):return
        if f.book_imbalance is not None and not math.isfinite(f.book_imbalance):return
        k=(f.venue,f.asset);h=self.hist[k]
        if h and f.ts_ms<=h[-1].ts_ms:return
        if h and f.ts_ms-h[-1].ts_ms>5000:h.clear()
        h.append(f)
        if len(h)<3:return
        prev=list(h)[-2]
        accel=f.return_bps-prev.return_bps
        vol_accel=f.volume_acceleration
        imbalance=f.book_imbalance if f.book_imbalance is not None else 0
        # Transparent research score, not calibrated probability.
        score=min(100,max(0,abs(accel)*2+max(0,vol_accel)*12+abs(imbalance)*18))
        if accel==0:return
        direction="BUY" if accel>0 else "SELL"
        if score<55:return
        key=("FAST",f.venue,f.asset,direction)
        if key in self.last and f.ts_ms-self.last[key]<10000:return
        self.last[key]=f.ts_ms
        row=DerivedSignal(f.ts_ms,f.asset,"FAST",direction,score,min(.99,score/100),
          {"venue":f.venue,"horizon":f.horizon,"return_bps":f.return_bps,"return_acceleration_bps":accel,
           "volume_acceleration":vol_accel,"book_imbalance":f.book_imbalance,"coverage_ms":f.coverage_ms,
           "confidence_semantics":"heuristic_score_not_calibrated_probability"})
        self.storage.append("derived_signal",asdict(row))
    def on_onchain(self,candidate):
        candidate=self.storage.payload(candidate) if not hasattr(candidate, "__dict__") else vars(candidate)
        if candidate.get("category")!="large_transfer":return
        amount=float(candidate.get("amount") or 0)
        if not math.isfinite(amount) or amount<=0:return
        # Existing collector threshold is already large; size raises evidence strength.
        score=min(100,55+15*max(0,math.log10(max(amount,1)/100)))
        evidence={"source":candidate["source"],"provider":candidate["provider"],"amount":amount,"tx_hash":candidate["tx_hash"],
          "classification":candidate.get("metadata",{}).get("label","unclassified"),
          "transaction":candidate.get("metadata",{}).get("transaction",{}),
          "classification_basis":candidate.get("classification_basis","unclassified"),
          "confidence_semantics":"heuristic_score_not_calibrated_probability",
          "causality":"large transfer observation only; wallet ownership/direction not established"}
        row=DerivedSignal(candidate["received_ts_ms"],candidate["asset"],"WHALE",candidate["direction"],score,
          min(.99,score/100),evidence)
        self.storage.append("derived_signal",asdict(row))
