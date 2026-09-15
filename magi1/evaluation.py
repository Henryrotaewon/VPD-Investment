"""Matured outcomes only; missing/stale prices never masquerade as returns."""
from .config import FORWARD_WINDOWS_SEC

def evaluate(shock_id,cohort,direction,entry,series,origin_ts_ms,asof_ms=None,tolerance_ms=5000):
    asof_ms=asof_ms if asof_ms is not None else max((t for t,p in series),default=origin_ts_ms)
    sign=1 if direction=='BUY' else -1; out=[]
    for horizon in FORWARD_WINDOWS_SEC:
        target=origin_ts_ms+horizon*1000
        if asof_ms<target: continue
        xs=sorted((t,p) for t,p in series if origin_ts_ms<=t<=target)
        coverage=bool(xs) and target-xs[-1][0]<=tolerance_ms
        if xs: coverage=coverage and max([xs[0][0]-origin_ts_ms]+[b[0]-a[0] for a,b in zip(xs,xs[1:])])<=tolerance_ms
        rets=[sign*(p/entry-1) for t,p in xs]
        ret=rets[-1] if coverage else None
        out.append({'shock_id':shock_id,'cohort':cohort,'horizon_sec':horizon,'event_ts_ms':target,'forward_return':ret,'mfe':max([0]+rets) if coverage else None,'mae':min([0]+rets) if coverage else None,'false_shock':ret<=0 if ret is not None else None,'status':'COMPLETE' if coverage else 'MISSING_DATA'})
    return out
