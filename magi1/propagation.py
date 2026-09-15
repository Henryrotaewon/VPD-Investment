from __future__ import annotations

from collections import defaultdict
from statistics import mean

from .schema import PropagationMeasurement


class PropagationStats:
    """Online empirical reception metrics; probabilities are never fabricated."""
    def __init__(self): self.samples=defaultdict(list)

    def observe(self, shock_id, asset, direction, origin, follower, origin_ts, reception_ts, origin_move_bps, follower_move_bps, origin_confidence):
        key=(asset,direction,origin,follower); self.samples[key].append(reception_ts is not None)
        received=sum(self.samples[key]); n=len(self.samples[key]); lag=reception_ts-origin_ts if reception_ts else None
        sensitivity=(follower_move_bps/origin_move_bps) if reception_ts and origin_move_bps else None
        return PropagationMeasurement(shock_id,asset,direction,origin,follower,origin_ts,reception_ts,origin_confidence,received/n,lag,sensitivity,max(0,lag) if lag is not None else None,n)
