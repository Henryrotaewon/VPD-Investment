"""Completed-hour, cost-adjusted profit protection for the hourly PAPER ledger."""
import json
import math

HOUR = 3600000
VERSION = 1
BREAKEVEN_AT = 6.0
TRAIL_AT = 15.0
MIN_GIVEBACK_PP = 6.0
ATR_MULTIPLIER = 3.0


def hourly_atr(rows, boundary, period=14):
    """Wilder ATR over the latest contiguous completed-hour suffix (up to 200 bars).

    Missing/no-trade candles are never synthesized. A previous close plus 14
    consecutive true ranges are required; future bars cannot affect the value.
    """
    bars = sorted((r for r in rows if r[0]+HOUR <= boundary), key=lambda r:r[0])[-200:]
    if not bars or bars[-1][0] != boundary-HOUR:
        return None
    start = 0
    for i in range(1, len(bars)):
        if bars[i][0]-bars[i-1][0] != HOUR:
            start = i
    bars = bars[start:]
    if len(bars) < period+1:
        return None
    ranges = [max(b[2]-b[3], abs(b[2]-a[4]), abs(b[3]-a[4]))
              for a,b in zip(bars,bars[1:])]
    atr = sum(ranges[:period])/period
    for value in ranges[period:]:
        atr = (atr*(period-1)+value)/period
    return atr


def breakeven_price(pos, state):
    return pos['cost']/(pos['qty']*(1-state['fee'])*(1-state['slip']))


def net_return(pos, state, price):
    return (price/breakeven_price(pos,state)-1)*100


def initialize(pos, source='entry'):
    pos['protection'] = dict(version=VERSION, source=source, peak_net_pct=None,
        peak_boundary=None, first_boundary=None, last_boundary=None,
        floor_net_pct=None, atr_1h=None, allowed_giveback_pp=None)


def update(pos, state, point):
    """Ratchet state once per post-entry close; this function never creates orders."""
    if 'protection' not in pos:
        initialize(pos)
    p = pos['protection']
    boundary = point['boundary']
    if boundary <= pos['entry_ms'] or boundary <= (p['last_boundary'] or 0):
        return
    value = net_return(pos,state,point['close'])
    if p['peak_net_pct'] is None or value > p['peak_net_pct']:
        p.update(peak_net_pct=value, peak_boundary=boundary)
    p['first_boundary'] = p['first_boundary'] or boundary
    p['last_boundary'] = boundary
    atr = point.get('atr_1h')
    p['atr_1h'] = atr if atr is not None and math.isfinite(atr) and atr >= 0 else None
    p['allowed_giveback_pp'] = (max(MIN_GIVEBACK_PP, ATR_MULTIPLIER*atr/breakeven_price(pos,state)*100)
                               if p['atr_1h'] is not None else None)
    peak = p['peak_net_pct']
    if peak >= BREAKEVEN_AT-1e-10:
        floor = max(0., p['floor_net_pct'] or 0.)
        if peak >= TRAIL_AT-1e-10 and p['allowed_giveback_pp'] is not None:
            floor = max(floor, peak-p['allowed_giveback_pp'])
        p['floor_net_pct'] = floor


def effective_stop(pos, state):
    floor = pos.get('protection',{}).get('floor_net_pct')
    return max(pos['initial_stop'], breakeven_price(pos,state)*(1+floor/100) if floor is not None else 0)


def exit_reason(pos, state, close):
    # Initial structure remains independently active, even above a profit floor.
    if close < pos['initial_stop']:
        return 'HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE'
    floor = pos.get('protection',{}).get('floor_net_pct')
    if floor is not None and close < effective_stop(pos,state):
        return 'HOURLY_PROFIT_TRAILING' if floor > 0 else 'HOURLY_BREAKEVEN_PROTECTION'
    return None


def restore(ledger, stamp):
    """One-time upgrade from recorded closes only; preserve balances and orders.

    Older observations have no ATR. They can restore the peak and breakeven
    activation; trailing requires a fresh, valid ATR. No historical exits replay.
    """
    for symbol,pos in ledger.s['positions'].items():
        if 'protection' in pos:
            continue
        initialize(pos,'stored_observations')
        rows = ledger.db.execute("SELECT payload FROM events WHERE kind='OBSERVATION' AND symbol=? AND ts<=? ORDER BY id",
                                 (symbol,stamp)).fetchall()
        points = [json.loads(raw) for (raw,) in rows]
        previous = ledger.s['previous'].get(symbol)
        if previous:
            points.append(previous)
        for point in sorted(points,key=lambda x:x['boundary']):
            if point['boundary'] <= stamp:
                update(pos,ledger.s,point)
        ledger.event(stamp,'PROTECTION_UPGRADE',symbol,pos['protection'])
    ledger.save()
