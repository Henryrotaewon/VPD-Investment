"""Offline, candle-only FAST case study; never fabricate historical books or fills.

Input is a frozen JSON(.gz) bundle, not exchange credentials. Bar close becomes
available only at bar end. Price probes are conditional on selection: historical
venue ranking, buy-flow and book depth are explicitly UNKNOWN.
"""
import argparse
from bisect import bisect_right, bisect_left
from collections import deque
from decimal import Decimal
import gzip
import hashlib
import json
from pathlib import Path
from statistics import median


def tick(price):
    """Upbit KRW schedule effective 2025-07-31, checked 2026-09-19 KST.

    Frozen research assumption. Not a current exchange metadata adapter.
    """
    p = Decimal(str(price))
    if not p.is_finite() or p <= 0:
        raise ValueError('INVALID_PRICE')
    for floor, step in [('1000000','1000'),('500000','500'),('100000','100'),
                        ('50000','50'),('10000','10'),('5000','5'),('1000','1'),
                        ('100','1'),('10','.1'),('1','.01'),('.1','.001'),
                        ('.01','.0001'),('.001','.00001'),('.0001','.000001'),
                        ('.00001','.0000001'),('0','.00000001')]:
        if p >= Decimal(floor):
            return Decimal(step)


def spread_floor_bps(bid):
    p = Decimal(str(bid))
    step = tick(p)
    if p % step:
        raise ValueError('OFF_TICK_GRID')
    return float(step / p * 10000)


def move_ticks(price, direction, count):
    p = Decimal(str(price))
    spread_floor_bps(p)  # Validate positive and on the frozen price grid.
    for _ in range(count):
        step = tick(p if direction > 0 else p-Decimal('.00000001'))
        p += step * direction
    return p


def validate(rows, duration_ms):
    previous = -1
    for r in rows:
        if r['ts'] <= previous or r['ts'] % duration_ms:
            raise ValueError('DUPLICATE_UNSORTED_OR_UNALIGNED_BAR')
        if not (0 < r['low'] <= min(r['open'], r['close']) <=
                max(r['open'], r['close']) <= r['high']):
            raise ValueError('INVALID_OHLC')
        previous = r['ts']


class ClosedBars:
    def __init__(self, rows, duration_ms):
        validate(rows, duration_ms)
        self.rows = rows
        self.ends = [r['ts'] + duration_ms for r in rows]

    def at(self, decision_ms):
        i = bisect_right(self.ends, decision_ms) - 1
        return self.rows[i] if i >= 0 else None


def scans(rows, start, end, interval_minutes=5, phase_minutes=0):
    """Completed minute-close proxy, not reconstructed ticker rankings."""
    bars = ClosedBars(rows, 60000)
    out = []
    for ts in range(start, end, 60000):
        if (ts // 60000 - phase_minutes) % interval_minutes:
            continue
        current, base = bars.at(ts), bars.at(ts-300000)
        if current is None or base is None:
            continue
        # Empty trade minutes do not become zero prices or invented volume.
        ret = (current['close']/base['close']-1)*10000
        out.append({'ts': ts, 'price': current['close'], 'return_5m_bps': ret,
                    'price_gate_pass': ret >= 100, 'rank_gate': 'UNKNOWN',
                    'tick_floor_at_reference_bps': spread_floor_bps(current['close']),
                    'price_age_ms': ts-(current['ts']+60000)})
    return out


def price_probes(seconds, selections, breakout_bps=10, chase_bps=100):
    """First close-price breakout in each hypothetical 15min watch.

    Fixed 30s prior high, >=30s observed history and <=3s gaps. Missing seconds
    mean no recorded trades, not a demonstrated absence of valid quotes. We reset
    this price proxy after such gaps, never synthesize a quote or aggressor side.
    Selections/probes overlap and MUST NOT be aggregated as independent trades.
    """
    validate(seconds, 1000)
    probes = []
    for selection in selections:
        if not selection['price_gate_pass']:
            continue
        history = deque()
        last = None
        hit = None
        for r in seconds:
            now = r['ts']+1000
            if now < selection['ts']:
                continue
            if now >= selection['ts']+900000:
                break
            if last is not None and now-last > 3000:
                history.clear()
            last = now
            while history and history[0][0] < now-31000:
                history.popleft()
            high = max((p for _, p in history), default=r['close'])
            if (history and now-history[0][0] >= 30000 and
                    (r['close']/high-1)*10000 >= breakout_bps and
                    (r['close']/selection['price']-1)*10000 <= chase_bps):
                hit = {'decision_ms': now, 'reference_price': r['close'],
                       'reference_tick_floor_bps': spread_floor_bps(r['close']),
                       'breakout_bps': (r['close']/high-1)*10000,
                       'chase_bps': (r['close']/selection['price']-1)*10000}
                break
            history.append((now, r['close']))
        probes.append({'selection_ms': selection['ts'], 'selection_price': selection['price'],
                       'probe': hit, 'full_fast_signal': 'NOT_ESTABLISHED'})
    return probes


def sensitivity(seconds, decision, delay_ms, horizon_ms, adverse_ticks=1, fee_bps=5):
    """Counterfactual price marks, never an execution backtest.

    Use the FIRST trade open in a second starting at/after decision+delay. Add
    one tick to entry and subtract one tick from exit when adverse_ticks=1.
    Prices may not be executable; no depth, TP/SL, capital or order matching.
    Exit horizon starts at the entry bar, each lookup waits at most 3 seconds.
    """
    if delay_ms < 0 or horizon_ms <= 0 or adverse_ticks not in (0,1) or not 0 <= fee_bps < 10000:
        raise ValueError('INVALID_SCENARIO')
    stamps = [r['ts'] for r in seconds]
    def next_open(target):
        i = bisect_left(stamps, target)
        if i == len(seconds) or stamps[i]-target > 3000:
            return None
        return seconds[i]
    entry = next_open(decision+delay_ms)
    exit_ = next_open(entry['ts']+horizon_ms) if entry else None
    base = {'delay_ms': delay_ms, 'horizon_ms': horizon_ms,
            'adverse_ticks_per_side': adverse_ticks, 'assumed_fee_bps_per_side': fee_bps,
            'mode': 'COUNTERFACTUAL_PRICE_MARK', 'execution_eligible': False}
    if not entry or not exit_:
        return dict(base, status='MISSING_TRADE_BAR', net_bps=None)
    buy = move_ticks(entry['open'], 1, adverse_ticks)
    sell = move_ticks(exit_['open'], -1, adverse_ticks)
    fee = Decimal(str(fee_bps))/10000
    value = (sell*(1-fee)/(buy*(1+fee))-1)*10000
    return dict(base, status='PRICE_MARK_ONLY', entry_ms=entry['ts'], exit_ms=exit_['ts'],
                entry_reference=entry['open'], exit_reference=exit_['open'],
                assumed_buy=float(buy), assumed_sell=float(sell), net_bps=float(value))


def study(bundle):
    minutes, seconds = bundle['upbit_minutes'], bundle['upbit_seconds']
    start, end = bundle['start_ms'], bundle['end_ms']
    day = [r for r in minutes if start <= r['ts'] < end]
    validate(minutes, 60000)
    validate(seconds, 1000)
    phases = {str(p): scans(minutes, start, end, phase_minutes=p) for p in range(5)}
    probes = price_probes(seconds, phases['0'])
    for p in probes:
        if p['probe']:
            p['sensitivity'] = [sensitivity(seconds, p['probe']['decision_ms'], d, h, t)
                                for d in (1000, 3000) for h in (60000,180000)
                                for t in (0,1)]
    # Every passing phase-0 scan in the two predeclared data windows; do not
    # select only the profitable marks or silently drop missing observations.
    scan_marks = []
    for s in phases['0']:
        if not s['price_gate_pass']:
            continue
        if not any(lo <= s['ts'] < hi for lo, hi in bundle['second_windows_ms']):
            continue
        scan_marks.append({'scan_ms':s['ts'], 'selection_price':s['price'],
                           'full_fast_signal':'NOT_ESTABLISHED',
                           'outcomes':[sensitivity(seconds,s['ts'],d,h,t)
                                       for d in (1000,3000) for h in (60000,180000)
                                       for t in (0,1)]})
    floor = [spread_floor_bps(r['close']) for r in day]
    return {'mode':'OFFLINE_CASE_STUDY', 'profitability_verdict':'NOT_VALIDATED',
            'full_strategy_backtest':'UNAVAILABLE', 'live_execution_enabled':False,
            'period':{'start_ms':start,'end_ms_exclusive':end},
            'bars': {'minute_count':len(day), 'missing_trade_minutes':(end-start)//60000-len(day),
                     'second_count_in_selected_windows':len(seconds)},
            'day': {'open':day[0]['open'],'high':max(r['high'] for r in day),
                    'low':min(r['low'] for r in day),'close':day[-1]['close'],
                    'peak_first_ms':next(r['ts'] for r in day if r['high']==max(x['high'] for x in day)),
                    'quote_volume':sum(r['quote_volume'] for r in day)},
            'tick_gate': {'threshold_bps':10,'reference_min_bps':min(floor),
                          'reference_max_bps':max(floor),'reference_median_bps':median(floor),
                          'reference_bars_with_floor_le_10bps':sum(x<=10 for x in floor),
                          'note':'Floor if each close were a bid; historical spread is unobserved.'},
            'scan_phase_price_proxies':phases,'conditional_probes':probes,
            'conditional_scan_marks':scan_marks,
            'limitations':[
                'G and the windows were selected after the surge; no unbiased strategy performance estimate.',
                'No historical venue universe/ranks, bid/ask depth, aggressor ticks or reception times.',
                'Bar times are exchange time; prices become known at close, plus explicit scenario delay.',
                'Price probes assume watch selection, ignore watch slots/cooldowns and may overlap.',
                'No trade is counted: conditional price marks are not simulated filled orders.',
                'Fixed horizons do not reproduce TP/SL, trailing, repeated orders or portfolio limits.',
                'Do not infer an announcement, whale actor or causal lead from price coincidence.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    args = parser.parse_args()
    if Path(args.input).resolve() == Path(args.output).resolve():
        parser.error('input and output must differ')
    opener = gzip.open if args.input.endswith('.gz') else open
    with opener(args.input,'rt',encoding='utf-8') as src:
        result = study(json.load(src))
    result['input_sha256'] = hashlib.sha256(Path(args.input).read_bytes()).hexdigest()
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    print(json.dumps({'verdict':result['profitability_verdict'],'day':result['day'],
                      'tick_gate':result['tick_gate']}))


if __name__ == '__main__':
    main()
