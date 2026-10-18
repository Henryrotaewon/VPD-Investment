"""Causal native-perpetual five-minute short hypotheses, not fitted forecasts."""
import math
import statistics

FIVE=300000
NAMES={'SHORT_BREAKDOWN':'하락 추세 돌파 숏','SHORT_RETEST':'반등 실패 숏'}


def ema(values, period):
    out=[];value=values[0]
    for x in values:
        value += 2/(period+1)*(x-value)
        out.append(value)
    return out


def evaluate(rows, now, regime):
    """Rows: native venue [open-ms,o,h,l,c,base-volume,quote-turnover]."""
    rows=sorted(r for r in rows if r[0]+FIVE<=now-2500)
    if len(rows)<65:
        return dict(ready=False,reason='NATIVE_PERP_HISTORY_SHORT')
    rows=rows[-100:]
    if any(b[0]-a[0]!=FIVE for a,b in zip(rows,rows[1:])):
        return dict(ready=False,reason='NATIVE_PERP_HISTORY_GAP')
    if not 2500<=now-(rows[-1][0]+FIVE)<FIVE+2500:
        return dict(ready=False,reason='NATIVE_PERP_HISTORY_STALE')
    for r in rows:
        if (len(r)!=7 or not all(math.isfinite(float(x)) for x in r)
            or min(r[1:5])<=0 or min(r[5:])<0 or not r[3]<=min(r[1],r[4])<=max(r[1],r[4])<=r[2]):
            return dict(ready=False,reason='INVALID_NATIVE_PERP_CANDLE')
    close=[r[4] for r in rows]
    e20,e50=ema(close,20),ema(close,50)
    ranges=[max(r[2]-r[3],abs(r[2]-p[4]),abs(r[3]-p[4])) for p,r in zip(rows,rows[1:])]
    atr=statistics.fmean(ranges[-14:]);last=rows[-1]
    result=dict(ready=True,reason='NO_PATTERN',signal_bar=last[0],close=last[4],ema20=e20[-1],
                ema50=e50[-1],atr=atr,signals=[])
    if regime.get('state')!='DOWN' or not 0<=now-regime.get('ts',0)<360000:
        result['reason']='BTC_NOT_DOWN';return result
    if not (atr>0 and last[4]<e20[-1]<e50[-1] and e20[-1]<e20[-4]):
        result['reason']='NATIVE_TREND_NOT_DOWN';return result
    if e20[-1]-last[4]>2*atr:
        result['reason']='SHORT_EXTENDED';return result
    median=statistics.median(r[6] for r in rows[-21:-1])
    if median<=0:
        result['reason']='TURNOVER_REFERENCE_ZERO';return result
    breakdown=last[4]<min(r[3] for r in rows[-21:-1]) and last[4]<last[1] and last[6]>=1.5*median
    # A prior close actually broke its preceding six-bar low. A later bar
    # retests EMA20 and closes below the preceding low; no future pivot labels.
    previous_break=any(rows[i][4]<min(r[3] for r in rows[i-6:i]) for i in range(len(rows)-6,len(rows)-1))
    retest=(previous_break and last[2]>=e20[-1]-.2*atr and last[4]<rows[-2][3]
            and last[4]<last[1] and last[6]>=median)
    for model,qualifies in [('SHORT_RETEST',retest),('SHORT_BREAKDOWN',breakdown)]:
        if not qualifies:continue
        stop=max(r[2] for r in rows[-2:])+.2*atr
        distance=stop/last[4]-1
        if not .004<=distance<=.015:
            result['reason']='TECHNICAL_STOP_TOO_WIDE_OR_NARROW';continue
        result['signals'].append(dict(model=model,signal_bar=last[0],stop_price=stop,
            stop_distance=distance,target_distance=2*distance,max_hold_ms=7200000,
            turnover_ratio=last[6]/median,atr=atr,source='NATIVE_PERPETUAL_5M',version='bear-native-20261018-v1'))
    if result['signals']:result['reason']='QUALIFIED'
    return result
