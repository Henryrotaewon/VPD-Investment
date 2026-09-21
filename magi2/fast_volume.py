"""FAST v2 selection from completed one-minute turnover/price observations."""
from datetime import datetime, timezone
import math

VERSION='fast-volume-accel-v2'
MINUTE=60000
POLICY={'min_return_5m_bps':100,'min_turnover_ratio':2.,'min_acceleration_bps':0.,
        'scan_interval_seconds':60,'max_candle_probes':20,'signal_cooldown_ms':600000,
        'spread_filter':False,'volume_basis':'EXECUTED_QUOTE_TURNOVER',
        'closed_candle_settlement_ms':3000}


def parse_candles(venue,data):
    if venue in ('upbit','bithumb'):
        return [dict(start_ms=int(datetime.fromisoformat(x['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000),
                     close=float(x['trade_price']),turnover=float(x['candle_acc_trade_price'])) for x in data]
    if venue=='binance':
        return [dict(start_ms=int(x[0]),close=float(x[4]),turnover=float(x[7])) for x in data]
    if venue=='kraken':
        return [dict(start_ms=int(x[0])*1000,close=float(x[4]),turnover=float(x[5])*float(x[6]))
                for k,rows in data['result'].items() if k!='last' for x in rows]
    raise ValueError('UNKNOWN_VENUE')


def evaluate(candles,asof_ms):
    """No forming bars, missing-bar fill, relative-price spread or order-book gate."""
    end=(asof_ms-POLICY['closed_candle_settlement_ms'])//MINUTE*MINUTE
    selected={}
    for c in candles:
        ts=c['start_ms']
        if ts<end-11*MINUTE or ts>=end:continue
        if (isinstance(ts,bool) or int(ts)!=ts or ts%MINUTE or ts in selected or
            not math.isfinite(c['close']) or c['close']<=0 or
            not math.isfinite(c['turnover']) or c['turnover']<0):
            return dict(qualified=False,reason='INVALID_OR_DUPLICATE_CANDLE',window_end_ms=end)
        selected[ts]=c
    expected=list(range(end-11*MINUTE,end,MINUTE))
    if any(ts not in selected for ts in expected):
        return dict(qualified=False,reason='MISSING_CLOSED_MINUTES',window_end_ms=end)
    bars=[selected[ts] for ts in expected]
    previous=sum(c['turnover'] for c in bars[1:6]); recent=sum(c['turnover'] for c in bars[6:11])
    r0=(bars[5]['close']/bars[0]['close']-1)*10000
    r1=(bars[10]['close']/bars[5]['close']-1)*10000
    ratio=recent/previous if previous>0 else None
    reason=('NO_TURNOVER_BASELINE' if ratio is None else
            'PRICE_RISE_BELOW_1PCT' if r1+1e-8<POLICY['min_return_5m_bps'] else
            'PRICE_NOT_ACCELERATING' if r1-r0<=POLICY['min_acceleration_bps']+1e-8 else
            'TURNOVER_BELOW_2X' if ratio+1e-10<POLICY['min_turnover_ratio'] else None)
    return dict(qualified=reason is None,reason=reason,window_end_ms=end,
                return_5m_bps=r1,previous_return_5m_bps=r0,acceleration_bps=r1-r0,
                turnover_5m=recent,previous_turnover_5m=previous,turnover_ratio=ratio,
                last_close=bars[-1]['close'],volume_basis=POLICY['volume_basis'])


def preselect(ranked):
    """Fresh five-minute gain first; rank jumps and occupied watches cannot evict it."""
    eligible=[r for r in ranked if r['returns_bps']['5']>=POLICY['min_return_5m_bps']]
    eligible.sort(key=lambda r:(-r['returns_bps']['5'],r['symbol']))
    return eligible[:POLICY['max_candle_probes']],eligible[POLICY['max_candle_probes']:]
