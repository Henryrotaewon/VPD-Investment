from __future__ import annotations

from .config import FORWARD_WINDOWS_SEC
from .schema import EvaluationRecord


def evaluate(shock_id: str, cohort: str, direction: str, entry: float, series: list[tuple[int,float]], origin_ts_ms: int):
    sign=1 if direction=="BUY" else -1; out=[]
    for horizon in FORWARD_WINDOWS_SEC:
        xs=[p for ts,p in series if origin_ts_ms<=ts<=origin_ts_ms+horizon*1000]
        final=xs[-1] if xs else None
        returns=[sign*(p/entry-1) for p in xs]
        ret=sign*(final/entry-1) if final else None
        out.append(EvaluationRecord(shock_id,cohort,horizon,ret,max(returns) if returns else None,min(returns) if returns else None,(ret is not None and ret<=0)))
    return out
