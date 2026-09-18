"""Additive derived signals over existing MAGI1 observations.

Never mutates raw trade/book, FeatureEngine, formation, propagation or evaluation.
FAST detects rapid positive venue-local returns. WHALE is a WAVE context input, not a strategy.
"""
from collections import defaultdict,deque
from dataclasses import dataclass,asdict
import math

@dataclass(frozen=True)
class DerivedSignal:
    event_ts_ms:int; asset:str; signal_type:str; direction:str; score:float
    confidence:float; evidence:dict; version:str="derived-v3-fast-rise"

class DerivedSignalEngine:
    def __init__(self,storage,min_return_bps=20.0,min_volume_ratio=2.0):
        if not all(math.isfinite(x) and x>0 for x in (min_return_bps,min_volume_ratio)):raise ValueError("INVALID_FAST_THRESHOLDS")
        self.min_return_bps=min_return_bps;self.min_volume_ratio=min_volume_ratio
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
        # Acceleration alone must never classify a slowing decline as a riser.
        if f.return_bps<self.min_return_bps or f.volume_acceleration<self.min_volume_ratio:return
        score=min(100,50*f.return_bps/self.min_return_bps+25*f.volume_acceleration/self.min_volume_ratio)
        key=("FAST",f.venue,f.asset,"BUY")
        if key in self.last and f.ts_ms-self.last[key]<10000:return
        self.last[key]=f.ts_ms
        row=DerivedSignal(f.ts_ms,f.asset,"FAST","BUY",score,min(.99,score/100),
          {"venue":f.venue,"horizon":f.horizon,"return_bps":f.return_bps,
           "volume_acceleration":f.volume_acceleration,"volume_ratio":f.volume_acceleration,
           "book_imbalance":f.book_imbalance,"coverage_ms":f.coverage_ms,
           "nominal_window_seconds":10,"fast_rule_version":"fast-rise-v1",
           "min_return_bps":self.min_return_bps,"min_volume_ratio":self.min_volume_ratio,
           "role":"FAST_CANDIDATE","universe_scope":"WAVE_COMMON_ASSETS_ONLY",
           "confidence_semantics":"heuristic_score_not_calibrated_probability"})
        self.storage.append("derived_signal",asdict(row))
    def on_onchain(self,candidate):
        candidate=self.storage.payload(candidate) if not hasattr(candidate, "__dict__") else vars(candidate)
        if candidate.get("category")!="large_transfer":return
        amount=float(candidate.get("amount") or 0)
        if not math.isfinite(amount) or amount<=0:return
        # Existing collector threshold is already large; size raises evidence strength.
        score=min(100,55+15*max(0,math.log10(max(amount,1)/100)))
        evidence={"role":"WAVE_INPUT","parent_strategy":"WAVE","source":candidate["source"],"provider":candidate["provider"],"amount":amount,"tx_hash":candidate["tx_hash"],
          "classification":candidate.get("metadata",{}).get("label","unclassified"),
          "transaction":candidate.get("metadata",{}).get("transaction",{}),
          "classification_basis":candidate.get("classification_basis","unclassified"),
          "confidence_semantics":"heuristic_score_not_calibrated_probability",
          "causality":"large transfer observation only; wallet ownership/direction not established"}
        row=DerivedSignal(candidate["received_ts_ms"],candidate["asset"],"WHALE",candidate["direction"],score,
          min(.99,score/100),evidence)
        self.storage.append("derived_signal",asdict(row))
