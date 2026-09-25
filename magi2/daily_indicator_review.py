"""Completed-day feature review only. No approved trigger or order integration.

Keep signed indicators in native units; never percent-divide MACD or Williams.
Composite weights/scales must be supplied explicitly for review, with no defaults.
"""
import math
from magi2.fast_wave_indicator import DAY_MS, VENUES, day_offset, ema, indicators

VERSION = 'daily-indicator-review-v1'
COMPONENTS = ('macd_hist', 'rsi', 'williams', 'volume')


def levels(rows, rsi_signal_method=None):
    if rsi_signal_method not in (None, 'SMA', 'EMA'):
        raise ValueError('UNSUPPORTED_RSI_SIGNAL_METHOD')
    closes = [r.close for r in rows]
    fast, slow = ema(closes,12), ema(closes,26)
    line = [fast[i]-slow[i] for i in range(25,len(rows))]
    value = indicators(rows)  # Wilder RSI14, Williams14, EMA MACD12/26/9.
    value.update(macd=line[-1], macd_signal=ema(line,9)[-1], volume=rows[-1].volume)
    for period in (5,10,20):
        value[f'volume_ma{period}'] = sum(r.volume for r in rows[-period:])/period
    for period in (5,10,20,60,120):
        value[f'price_ma{period}'] = sum(closes[-period:])/period
    if rsi_signal_method:
        rsi = [indicators(rows[:i])['rsi'] for i in range(34,len(rows)+1)]
        value['rsi_signal9'] = sum(rsi[-9:])/9 if rsi_signal_method=='SMA' else ema(rsi,9)[-1]
    else:
        value['rsi_signal9'] = None  # Screenshot gives 9, not the smoothing method.
    return value


def composite_review(changes, weights, scales):
    """Explicit, provisional signed combination; never classifies a buy signal."""
    if set(weights)!=set(COMPONENTS) or set(scales)!=set(COMPONENTS):
        raise ValueError('FOUR_EXPLICIT_WEIGHTS_AND_SCALES_REQUIRED')
    if (any(not math.isfinite(x) or x<0 for x in weights.values()) or sum(weights.values())<=0
            or any(not math.isfinite(x) or x<=0 for x in scales.values())):
        raise ValueError('INVALID_WEIGHTS_OR_SCALES')
    return {part:sum(weights[k]*changes[k][part]/scales[k] for k in COMPONENTS)/sum(weights.values())
            for part in ('velocity','acceleration')}


def review(rows, asof_ms, *, venue, symbol, rsi_signal_method=None, weights=None, scales=None):
    if venue not in VENUES or not symbol: raise ValueError('INVALID_MARKET')
    # Exclude the forming and all future candles before any feature calculation.
    closed = [r for r in rows if r.open_ms+DAY_MS<=asof_ms]
    if len(closed)<122: raise ValueError('INSUFFICIENT_COMPLETED_HISTORY')
    for r in closed: r.validate(day_offset(venue))
    if any(b.open_ms-a.open_ms!=DAY_MS for a,b in zip(closed,closed[1:])):
        raise ValueError('NONCONTIGUOUS_OR_UNSORTED_HISTORY')
    points=[]
    for end in range(len(closed)-2,len(closed)+1):
        points.append(dict(day_ms=closed[end-1].open_ms, values=levels(closed[:end],rsi_signal_method)))
    a,b,c = [p['values'] for p in points]
    changes={}
    for k in c:
        if any(v[k] is None for v in (a,b,c)): continue
        previous=b[k]-a[k]; current=c[k]-b[k]
        changes[k]=dict(previous_velocity=previous,velocity=current,acceleration=current-previous)
    composite=None
    if weights is not None or scales is not None:
        if weights is None or scales is None: raise ValueError('EXPLICIT_WEIGHTS_AND_SCALES_REQUIRED')
        composite=composite_review(changes,weights,scales)
    boundary=closed[-1].open_ms+DAY_MS
    return dict(version=VERSION,venue=venue,symbol=symbol,signal_day_ms=closed[-1].open_ms,
                signal_available_ms=boundary,next_daily_open_ms=boundary,asof_ms=asof_ms,
                observed_after_open_ms=asof_ms-boundary,observations=points,changes=changes,
                composite=composite,weights=weights,scales=scales,rsi_method='WILDER14',
                rsi_signal_method=rsi_signal_method,signal=None,entry_enabled=False,
                policy_status='WEIGHTS_THRESHOLD_EXECUTION_AND_EXIT_UNAPPROVED',
                timing_note='D_CLOSE_THEN_D_PLUS_1_OPEN_REFERENCE_NOT_A_GUARANTEED_FILL')
