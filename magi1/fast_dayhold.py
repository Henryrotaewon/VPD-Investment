"""FAST-BASE day-hold PAPER policy. Never imports an exchange order client.

Thresholds are prospective trial settings, not fitted profitability claims.
"""
import asyncio
from datetime import datetime, timezone
import json
import math
import time

import aiohttp
from magi1.fast_paper import Paper, POLICY as FLOW_POLICY, DAY, walk

FIVE = 300_000
REGIME_AGE = 180_000
VERSION = 'fast-dayhold-20261023-v1'
RISK = {'UP': [.15, .04], 'NEUTRAL': [.12, .04], 'DOWN': [.08, .03], 'SHOCK': [.06, .02]}


def policy_from(previous):
    return dict(previous, version=VERSION + ('-' + previous['venue'] if previous.get('venue') else ''),
                no_new_high_ms=None, max_hold_ms=DAY, base_tp=.12, base_sl=.04,
                entry='FLOW_CONFIRMATION_2_FIRST_PRICE_CAP',
                protection='NET_SL_NEVER_LOOSEN', flow_exit='SL_APPROACH_AND_FAILED_RECOVERY',
                trail='BTC_REGIME_RISK_ONLY', gap_exit='REVALIDATE_FRESH_BOOK',
                risk_by_regime=RISK, warning_fraction=.75, warning_recovery_fraction=.60)


def risk_for(state):
    return RISK.get(state, RISK['NEUTRAL'])


def btc_state(candles, ts):
    """Only closed, contiguous 5m BTC candles; no partial-candle extrapolation."""
    rows = []
    for row in candles:
        start = int(datetime.fromisoformat(row['candle_date_time_utc']).replace(tzinfo=timezone.utc).timestamp()*1000)
        close = float(row['trade_price'])
        if start + FIVE <= ts:
            if not math.isfinite(close) or close <= 0:
                raise ValueError('BTC_INVALID_PRICE')
            rows.append((start, close))
    rows = sorted(rows)[-25:]
    if (len(rows) < 25 or len({r[0] for r in rows}) != 25 or
            any(b[0]-a[0] != FIVE for a,b in zip(rows, rows[1:])) or
            not 0 <= ts-(rows[-1][0]+FIVE) < FIVE):
        raise ValueError('BTC_INCOMPLETE_OR_STALE')
    ema = rows[0][1]
    for _, close in rows[1:]:
        ema = close*2/21 + ema*19/21
    last = rows[-1][1]
    r15, r60 = last/rows[-4][1]-1, last/rows[-13][1]-1
    state = ('SHOCK' if r15 <= -.01 or r60 <= -.02 else
             'UP' if r60 >= .003 and last > ema else
             'DOWN' if r60 <= -.003 and last < ema else 'NEUTRAL')
    return dict(state=state, ts=ts, bar_end=rows[-1][0]+FIVE, close=last,
                return_15m=r15, return_1h=r60, ema20=ema,
                source='UPBIT_BTC_COMPLETED_5M_1H_EMA20')


async def monitor_btc(session, observer, clock=None):
    """Bounded public-data poll, independent of stream processing and venue FX."""
    clock = clock or (lambda: time.time_ns()//1_000_000)
    previous = None
    while True:
        try:
            async with session.get('https://api.upbit.com/v1/candles/minutes/5',
                                   params={'market':'KRW-BTC','count':26},
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                value = btc_state(await response.json(), clock())
            # Two consecutive polls prevent ordinary regime flicker. Crash risk is immediate.
            if value['state'] == 'SHOCK' or value['state'] == previous:
                if observer.paper is not None:
                    observer.paper.set_regime(value)
            previous = value['state']
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            previous = None
            print(json.dumps(dict(mode='FAST_BTC_RISK_UNAVAILABLE', reason=type(exc).__name__)), flush=True)
            # Do not refresh a stale timestamp or invent a neutral market.
        await asyncio.sleep(60)


def early_evidence(features):
    """Observation only: a strong first window does not authorize a purchase."""
    thresholds = [('return_10s_bps', 0, '>'), ('price_acceleration_bps', 0, '>'),
                  ('turnover_change_ratio', 1.2, '>='), ('buy_share_change', 0, '>='),
                  ('ofi_change', 0, '>'), ('rise_from_60s_low_bps', 300, '<='),
                  ('spread_bps', 50, '<=')]
    reasons = []
    for key, value, op in thresholds:
        actual = features.get(key)
        ok = actual is not None and math.isfinite(actual)
        if ok:
            ok = actual > value if op == '>' else actual >= value if op == '>=' else actual <= value
        if not ok:
            reasons.append(key)
    return dict(eligible=not reasons, missing_or_failed=reasons, mode='OBSERVE_ONLY')


class DayHoldPaper(Paper):
    def __init__(self, path, stamp, legacy_policy=None):
        self.regime = {'state':'UNAVAILABLE','ts':0}
        super().__init__(path, stamp, policy_from(legacy_policy or FLOW_POLICY))
        self.db.execute('''CREATE TABLE IF NOT EXISTS early_candidates(
            symbol TEXT, day INTEGER, ts INTEGER, payload TEXT, PRIMARY KEY(symbol,day))''')
        # A resumed process must fetch current BTC data before authorizing a new entry.
        for symbol,p in self.s['positions'].items():
            if 'risk' not in p:
                self.configure_position(symbol, stamp, 'NEUTRAL', preserve_stop=True)
        self.save(stamp)

    def migrate_policy(self, previous, stamp):
        # Only known FAST BASE policies; never silently migrate BEAR/experiments.
        suffix = '-' + previous['venue'] if previous.get('venue') else ''
        expected = dict(FLOW_POLICY)
        if suffix:
            for key in ('venue','currency','initial','initial_krw','initial_fx_krw','fee','min_budget'):
                expected[key] = previous[key]
            expected['version'] += suffix
        if previous != expected:
            raise ValueError('FAST_DAYHOLD_UNSUPPORTED_MIGRATION')
        anchor = dict(ts=stamp, equity=self.s['cash']+sum(p['qty']*p['mark'] for p in self.s['positions'].values()),
                      realized=self.s['realized'], closed=self.s['closed'], winning=self.s['winning'],
                      previous=previous, current=self.policy)
        self.s.setdefault('policy_history', []).append(anchor)
        self.s['dayhold_start'] = anchor
        self.s['policy'] = dict(self.policy)
        # Existing fills/cash/performance anchors and user pause state remain intact.
        self.event(stamp, 'DAYHOLD_POLICY_MIGRATED', '', anchor)

    def regime_at(self, ts):
        return self.regime['state'] if 0 <= ts-self.regime['ts'] <= REGIME_AGE else 'UNAVAILABLE'

    def set_regime(self, value):
        if value['state'] not in RISK or value['ts'] < self.regime['ts']:
            return
        changed = value['state'] != self.regime['state']
        self.regime = dict(value)
        if changed:
            self.event(value['ts'], 'BTC_RISK_REGIME', '', value)
        for symbol in list(self.s['positions']):
            self.configure_position(symbol, value['ts'], value['state'], preserve_stop=True)
        self.save(value['ts'])

    def observe_early(self, symbol, ts, features):
        if self.s.get('entries_paused') or features.get('confirmation_delay_ms') != 0:
            return
        record = dict(features, early=early_evidence(features), regime=self.regime_at(ts))
        self.db.execute('INSERT OR IGNORE INTO early_candidates VALUES(?,?,?,?)',
                        (symbol, ts//DAY, ts, json.dumps(record, allow_nan=False)))
        # One row per symbol/day, seven days; no additional raw tick storage.
        self.db.execute('DELETE FROM early_candidates WHERE day<?', (ts//DAY-7,))

    def signal(self, symbol, ts, price, low, features):
        regime = self.regime_at(ts)
        if regime in ('UNAVAILABLE','SHOCK'):
            if self.can_signal(symbol, ts):
                self.db.execute('INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?)',
                    (symbol, ts//DAY, ts, 'SKIPPED', 'BTC_'+regime, json.dumps(features)))
                self.save(ts)
            return
        # Keep two-window confirmation, but never reset the cap to the inflated second price.
        reference = features.get('first_condition_price') or price
        if not math.isfinite(reference) or reference <= 0:
            return
        tp, sl = risk_for(regime)
        stop = reference*(1-sl)
        super().signal(symbol, ts, reference, stop,
                       dict(features, btc_regime=dict(self.regime), take_profit=tp, stop_loss=sl))

    def entry_rejection(self, order, price):
        # on_book checks freshness at the actual fill time; a newer risk snapshot
        # must not be compared to the earlier signal timestamp.
        regime = self.regime['state']
        return 'BTC_'+regime if regime in ('UNAVAILABLE','SHOCK') else ''

    def configure_position(self, symbol, ts, regime, preserve_stop):
        p = self.s['positions'][symbol]
        tp, sl = risk_for(regime)
        prior = p.get('risk', {})
        sl = min(sl, prior.get('sl', sl))
        # Stop is a net-liquidation trigger after modeled sell costs, not a guaranteed fill.
        p.setdefault('original_qty', p['original_cost']/(p['entry_price']*(1+self.policy['fee'])))
        unit_cost = p['original_cost']/p['original_qty']
        stop = unit_cost*(1-sl)/((1-self.policy['slip'])*(1-self.policy['fee']))
        if preserve_stop:
            stop = max(p['stop'], stop)
        changed = not prior or (prior['tp'], prior['sl'], prior['regime']) != (tp, sl, regime)
        p['stop'] = stop
        p['risk'] = dict(tp=tp, sl=sl, regime=regime)
        p.setdefault('warning', None)
        if changed:
            self.event(ts, 'DAYHOLD_RISK_SET', symbol, dict(p['risk'], stop=stop, deadline=p['entry_ms']+DAY))

    def position_opened(self, symbol, ts, order):
        p = self.s['positions'][symbol]
        p['original_qty'] = p['qty']
        self.configure_position(symbol, ts, self.regime_at(ts), preserve_stop=False)

    def request_exit(self, symbol, ts, reason):
        super().request_exit(symbol, ts, 'DAYHOLD_HARD_SL' if reason == 'PROTECTION' else reason)

    def gap(self, symbols, ts, reason):
        for symbol in list(symbols):
            self.discard_exit_review(symbol, ts, reason)
            self.books.pop(symbol, None)
            self.trades.pop(symbol, None)
            self.cancel(symbol, ts, 'DATA_GAP_'+reason)
            p = self.s['positions'].get(symbol)
            if p:
                p['uncertain'] = True
                p['negative_windows'] = 0
                p['flow_weak'] = False
                p['flow_ts'] = 0
                if p.get('diagnostic'):
                    p['diagnostic']['continuous'] = False
                self.event(ts, 'DAYHOLD_DATA_GAP', symbol, dict(reason=reason))
        self.save(ts)

    def on_book(self, symbol, ts, book):
        if not 0 <= ts-book['stamp'] <= self.policy['quote_age_ms']:
            return
        if symbol in self.s['pending'] and self.regime_at(ts) in ('UNAVAILABLE','SHOCK'):
            self.cancel(symbol, ts, 'BTC_'+self.regime_at(ts))
        p = self.s['positions'].get(symbol)
        if p:
            if book['stamp'] <= p.get('dayhold_book_stamp', -1):
                return
            p['dayhold_book_stamp'] = book['stamp']
            p['uncertain'] = False
            _, raw, remaining = walk(book['bids'], quantity=p['qty'])
            if remaining <= 1e-8:
                net = raw*(1-self.policy['slip'])*(1-self.policy['fee'])
                ret = (p['total_pnl']+net-p['cost'])/p['original_cost']
                sl, tp = p['risk']['sl'], p['risk']['tp']
                if ret <= -sl:
                    self.request_exit(symbol, ts, 'DAYHOLD_HARD_SL')
                elif ret >= tp:
                    self.request_exit(symbol, ts, 'DAYHOLD_TAKE_PROFIT')
                elif ret <= -sl*.75:
                    if p.get('warning') is None:
                        p['warning'] = dict(ts=ts, return_pct=ret*100)
                        self.event(ts, 'DAYHOLD_SL_PREPARE', symbol, p['warning'])
                    if (ts-p['warning']['ts'] >= 20000 and p.get('flow_weak') and
                            0 <= ts-p.get('flow_ts',0) <= 12000):
                        self.request_exit(symbol, ts, 'DAYHOLD_SL_APPROACH_FAILED_RECOVERY')
                elif ret > -sl*.60 and p.get('warning') is not None:
                    p['warning'] = None
                    self.event(ts, 'DAYHOLD_SL_RECOVERED', symbol, dict(return_pct=ret*100))
            if ts-p['entry_ms'] >= DAY:
                self.request_exit(symbol, ts, 'DAYHOLD_24H_LIMIT')
        super().on_book(symbol, ts, book)

    def window(self, symbol, end, bars, decision_ms=None):
        p = self.s['positions'].get(symbol)
        if not p:
            return
        current = next((b for b in bars if b['t'] == end-10000), None)
        previous = next((b for b in bars if b['t'] == end-20000), None)
        if current and current['t'] >= p['entry_ms']:
            negative = current['buy']*2-current['value'] < 0
            p['negative_windows'] = p['negative_windows']+1 if negative else 0
        else:
            p['negative_windows'] = 0
        p['flow_ts'] = end
        p['flow_weak'] = bool(p['negative_windows'] >= 2 and current and previous and
            current.get('c') and previous.get('c') and current['c'] < previous['c'] and
            p['volume'] > 0 and current['c'] < p['value']/p['volume'])
        # No short trailing stop, 20s standalone sale, or 3m timeout.

    def tick(self, ts):
        for symbol,p in list(self.s['positions'].items()):
            if ts-p['entry_ms'] >= DAY:
                self.request_exit(symbol, ts, 'DAYHOLD_24H_LIMIT')
        super().tick(ts)

    def report(self, ts):
        report = super().report(ts)
        anchor = self.s.get('dayhold_start')
        segment = dict(started_ms=anchor['ts'], equity=anchor['equity'],
                       return_pct=(report['equity']/anchor['equity']-1)*100,
                       closed=self.s['closed']-anchor['closed']) if anchor and anchor['equity'] else None
        return dict(report, btc_regime=dict(self.regime, effective_state=self.regime_at(ts)),
                    max_hold_hours=24, risk_by_regime=RISK, dayhold_segment=segment,
                    early_entry_mode='OBSERVE_ONLY',
                    sl_preparing=sum(p.get('warning') is not None for p in self.s['positions'].values()))
