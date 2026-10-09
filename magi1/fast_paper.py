"""Causal FAST PAPER ledger. Public quotes only; no order client.

The signal observer owns this object and its SQLite connection on one thread.
Account and fill changes commit atomically; never replay historical captures.
"""
import json
from pathlib import Path
import sqlite3

DAY = 86_400_000
POLICY = dict(version='fast-flow-paper-v2', initial=3000000., slots=10,
              fee=.0005, slip=.0005, entry_cap=.015, entry_wait_ms=10000,
              quote_age_ms=2000, no_new_high_ms=180000,
              entry='FLOW_CONFIRMATION_A', protection='PRIOR_60S_LOW',
              flow_exit='TWO_10S_NET_SELL_AND_BELOW_ENTRY_VWAP',
              trail='THREE_COMPLETED_MINUTE_LOWS_RATCHET',
              gap_exit='FIRST_FRESH_BOOK_AFTER_GAP', same_day_reentry=False,
              retry_unfilled=True)
LEGACY_POLICY = dict(POLICY, version='fast-flow-paper-v1', entry_cap=.005)
LEGACY_POLICY.pop('retry_unfilled')


def walk(levels, *, budget=None, quantity=None):
    """Consume visible depth once; return quantity, notional, unfilled target."""
    remaining = budget if budget is not None else quantity
    qty = value = 0.
    for price, size in levels:
        take = min(size, remaining / price if budget is not None else remaining)
        qty += take
        value += take * price
        remaining -= take * price if budget is not None else take
        if remaining <= 1e-8:
            break
    return qty, value, max(0., remaining)


class Paper:
    def __init__(self, path, stamp, policy=None):
        self.policy = dict(POLICY if policy is None else policy)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,payload TEXT);
          CREATE TABLE IF NOT EXISTS signals(symbol TEXT,day INTEGER,ts INTEGER,
            status TEXT,reason TEXT,payload TEXT,PRIMARY KEY(symbol,day));
          CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,ts INTEGER,symbol TEXT,
            side TEXT,quantity REAL,price REAL,fee REAL,cash REAL,pnl REAL,
            reason TEXT,decision_ms INTEGER);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts INTEGER,kind TEXT,
            symbol TEXT,payload TEXT);
          CREATE TABLE IF NOT EXISTS daily(day INTEGER PRIMARY KEY,ts INTEGER,payload TEXT);
          CREATE INDEX IF NOT EXISTS fills_buy_day ON fills(symbol,side,ts);
        ''')
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(
            policy=self.policy, started_ms=stamp, updated_ms=stamp, cash=self.policy['initial'],
            realized=0., positions={}, pending={}, closed=0, winning=0)
        if self.s['policy'] == LEGACY_POLICY and self.policy == POLICY:
            self.event(stamp, 'POLICY_UPDATED', '', dict(previous=self.s['policy'], current=self.policy))
            self.s['policy'] = dict(self.policy)
        elif self.s['policy'] != self.policy:
            raise ValueError('FAST_PAPER_POLICY_MISMATCH')
        self.books = {}
        self.trades = {}
        self.diagnostics = False
        self.last_checkpoint = stamp
        if row:
            # No invented prices or continuity across process downtime.
            self.gap(set(self.s['pending']) | set(self.s['positions']) | set(self.s.get('exit_reviews', {})), stamp, 'RESTART')
        self.event(stamp, 'START', '', dict(policy=self.policy, restored=bool(row)))
        self.save(stamp)

    def event(self, ts, kind, symbol, payload):
        self.db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                        (ts, kind, symbol, json.dumps(payload, allow_nan=False)))

    def save(self, ts):
        self.s['updated_ms'] = ts
        if self.s['cash'] < -1e-6 or len(self.s['positions']) + len(self.s['pending']) > self.policy['slots']:
            raise RuntimeError('FAST_PAPER_CAPITAL_INVARIANT')
        reserved = sum(x['budget'] for x in self.s['pending'].values())
        if reserved > self.s['cash'] + 1e-6:
            raise RuntimeError('FAST_PAPER_RESERVATION_INVARIANT')
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',
                        (json.dumps(self.s, allow_nan=False),))
        self.db.commit()

    def bought_today(self, symbol, ts):
        start = ts // DAY * DAY
        return bool(self.db.execute("SELECT 1 FROM fills WHERE symbol=? AND side='BUY' AND ts>=? AND ts<? LIMIT 1",
                                    (symbol, start, start+DAY)).fetchone())

    def can_signal(self, symbol, ts):
        return (not self.s.get('entries_paused',False) and
                symbol not in self.s['positions'] and symbol not in self.s['pending']
                and not self.bought_today(symbol, ts))

    def pause_entries(self, ts):
        """Persistently stop new trades; retain accounting and protective exits."""
        if not self.s.get('entries_paused',False):
            self.s.update(entries_paused=True,entries_paused_ms=ts)
            self.event(ts,'USER_PAUSED','',dict(positions=list(self.s['positions'])))
        for symbol in list(self.s['pending']):self.cancel(symbol,ts,'USER_STOPPED')
        self.save(ts)

    def resume_entries(self, ts, request_id):
        """One authorized restart, preserving cash, history and daily BUY lockout."""
        if self.s.get('resume_request_id') == request_id:
            return False
        if not request_id or self.s['positions'] or self.s['pending'] or self.s['cash'] <= 0:
            raise ValueError('FAST_RESUME_REQUIRES_FLAT_ACCOUNT_AND_REQUEST_ID')
        segment = dict(request_id=request_id, started_ms=ts, equity=self.s['cash'],
                       realized=self.s['realized'], closed=self.s['closed'], winning=self.s['winning'])
        self.s.update(entries_paused=False, resume_request_id=request_id, review_segment=segment)
        self.event(ts, 'USER_RESUMED', '', segment)
        self.save(ts)
        return True

    def signal(self, symbol, ts, price, low, features):
        if ts < self.s['started_ms']:
            return
        day = ts // DAY
        previous = self.db.execute('SELECT ts,status,reason,payload FROM signals WHERE symbol=? AND day=?', (symbol, day)).fetchone()
        if not self.can_signal(symbol, ts) or (previous and previous[0] >= ts):
            return
        positions, pending = self.s['positions'], self.s['pending']
        vacant = self.policy['slots'] - len(positions) - len(pending)
        free = self.s['cash'] - sum(x['budget'] for x in pending.values())
        budget = free / vacant if vacant > 0 else 0.
        reason = ('ALREADY_HELD' if symbol in positions or symbol in pending else
                  'NO_SLOT' if vacant <= 0 else 'INSUFFICIENT_CASH' if budget < 5000 else
                  'INVALID_PROTECTION' if low is None or low <= 0 or low >= price else '')
        if previous:
            self.event(ts, 'PRIOR_SIGNAL', symbol, dict(ts=previous[0], status=previous[1],
                       reason=previous[2], payload=json.loads(previous[3])))
        self.db.execute('INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?)',
                        (symbol, day, ts, 'SKIPPED' if reason else 'PENDING', reason,
                         json.dumps(dict(features, reference=price, protection=low, budget=budget))))
        if not reason:
            pending[symbol] = dict(ts=ts, day=day, reference=price, stop=low,
                                   budget=budget, last_reason='WAIT_FRESH_BOOK')
            if self.diagnostics:
                pending[symbol]['diagnostic'] = dict(features)
        self.save(ts)

    def cancel(self, symbol, ts, reason):
        order = self.s['pending'].pop(symbol, None)
        if order:
            self.event(ts, 'ENTRY_CANCELED', symbol, dict(order, reason=reason))
            self.db.execute("UPDATE signals SET status='SKIPPED',reason=? WHERE symbol=? AND day=?",
                            (reason, symbol, order['day']))
            self.save(ts)

    def request_exit(self, symbol, ts, reason):
        p = self.s['positions'].get(symbol)
        if p and not p.get('exit'):
            p['exit'] = dict(ts=ts, reason=reason)
            self.event(ts, 'EXIT_DECISION', symbol, p['exit'])
            self.save(ts)

    def gap(self, symbols, ts, reason):
        for symbol in list(symbols):
            self.discard_exit_review(symbol, ts, reason)
            self.books.pop(symbol, None)
            self.trades.pop(symbol, None)
            self.cancel(symbol, ts, 'DATA_GAP_' + reason)
            p = self.s['positions'].get(symbol)
            if p:
                p['uncertain'] = True
                if p.get('diagnostic'):
                    p['diagnostic']['continuous'] = False
                self.request_exit(symbol, ts, 'DATA_GAP_' + reason)
        self.save(ts)

    def on_trade(self, symbol, ts, price, quantity, stamp):
        if not 0 <= ts - stamp <= self.policy['quote_age_ms']:
            return
        previous = self.trades.get(symbol)
        if previous and stamp < previous[2]:
            return
        self.trades[symbol] = (ts, price, stamp)
        p = self.s['positions'].get(symbol)
        if not p or stamp < p['entry_ms']:
            return
        p['value'] += price * quantity
        p['volume'] += quantity
        if price > p['peak']:
            p['peak'] = price
            p['peak_ms'] = ts
        if price <= p['stop']:
            self.request_exit(symbol, ts, 'PROTECTION')

    def on_book(self, symbol, ts, book):
        if not 0 <= ts - book['stamp'] <= self.policy['quote_age_ms']:
            return
        self.books[symbol] = book
        self.review_after_exit(symbol, ts, book)
        p = self.s['positions'].get(symbol)
        if p:
            self.mark_diagnostic(p, book)
            p['mark'] = book['bp'] * (1 - self.policy['slip']) * (1 - self.policy['fee'])
            p['mark_ms'] = ts
            if book['bp'] <= p['stop']:
                self.request_exit(symbol, ts, 'PROTECTION')
            timeout = self.policy['no_new_high_ms']
            if timeout is not None and not p.get('exit') and ts - p['peak_ms'] >= timeout:
                if p['qty'] * p['mark'] <= p['cost']:
                    self.request_exit(symbol, ts, 'NO_NEW_HIGH_3M_NONPOSITIVE')
            self.execute_exit(symbol, ts, book)
        order = self.s['pending'].get(symbol)
        if not order:
            return
        if self.s.get('entries_paused',False):
            self.cancel(symbol,ts,'USER_STOPPED')
            return
        if ts > order['ts'] + self.policy['entry_wait_ms']:
            self.cancel(symbol, ts, order['last_reason'])
            return
        # Both receive time and exchange timestamp must follow the decision.
        trade = self.trades.get(symbol)
        if ts <= order['ts'] or book['stamp'] <= order['ts'] or not trade or not 0 <= ts - trade[0] <= 2000:
            return
        if book['bp'] <= order['stop']:
            self.cancel(symbol, ts, 'PROTECTION_ALREADY_BROKEN')
            return
        if self.bought_today(symbol, ts):
            self.cancel(symbol, ts, 'ALREADY_BOUGHT_TODAY')
            return
        raw_budget = order['budget'] / ((1 + self.policy['fee']) * (1 + self.policy['slip']))
        qty, raw, remaining = walk(book['asks'], budget=raw_budget)
        if remaining > 1e-6 or qty <= 0:
            order['last_reason'] = 'INSUFFICIENT_VISIBLE_DEPTH'
            return
        price = raw / qty * (1 + self.policy['slip'])
        if price > order['reference'] * (1 + self.policy['entry_cap']):
            order['last_reason'] = 'ENTRY_PRICE_CAP'
            order['last_expected_price'] = price
            return
        rejection = self.entry_rejection(order, price)
        if rejection:
            self.cancel(symbol, ts, rejection)
            return
        cost = order['budget']
        self.s['cash'] -= cost
        self.s['positions'][symbol] = dict(qty=qty, cost=cost, original_cost=cost,
            entry_ms=ts, entry_price=price, stop=order['stop'], peak=trade[1], peak_ms=ts,
            value=0., volume=0., negative_windows=0, minute_lows=[], exit=None,
            mark=book['bp'] * (1-self.policy['slip']) * (1-self.policy['fee']), mark_ms=ts,
            uncertain=False, total_pnl=0., last_sell_stamp=-1)
        if self.diagnostics:
            evidence = order.get('diagnostic', {})
            p = self.s['positions'][symbol]
            p['diagnostic'] = dict(entry_ms=ts, decision_ms=order['ts'],
                original_qty=qty,
                fill_delay_ms=ts-order['ts'], continuous=True, best_exit_net_pct=None,
                worst_exit_net_pct=None, depth_missing_books=0,
                price_acceleration_bps=evidence.get('price_acceleration_bps'),
                confirmation_delay_ms=evidence.get('confirmation_delay_ms'),
                confirmation_rise_bps=evidence.get('confirmation_rise_bps'),
                entry_rise_bps=(price/order['reference']-1)*10000,
                stop_distance_bps=(1-order['stop']/price)*10000,
                spread_bps=(book['ap']/book['bp']-1)*10000)
            self.mark_diagnostic(p, book)
            p['diagnostic']['immediate_exit_net_pct'] = p['diagnostic']['worst_exit_net_pct']
            self.event(ts, 'ENTRY_DIAGNOSTIC', symbol, p['diagnostic'])
        self.db.execute('INSERT INTO fills(ts,symbol,side,quantity,price,fee,cash,pnl,reason,decision_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (ts, symbol, 'BUY', qty, price, qty*price*self.policy['fee'], -cost, 0., self.policy['entry'], order['ts']))
        self.db.execute("UPDATE signals SET status='BOUGHT',reason='' WHERE symbol=? AND day=?", (symbol, order['day']))
        del self.s['pending'][symbol]
        self.save(ts)

    def entry_rejection(self, order, price):
        """Optional strategy-specific check on the depth-weighted fill price."""
        return ''

    def mark_diagnostic(self, position, book):
        """Depth- and cost-adjusted hypothetical liquidation, never a fill."""
        d = position.get('diagnostic')
        if not d or book['stamp'] <= position['last_sell_stamp']:
            return
        _, raw, remaining = walk(book['bids'], quantity=position['qty'])
        if remaining > 1e-8:
            d['depth_missing_books'] += 1
            return
        net = raw*(1-self.policy['slip'])*(1-self.policy['fee'])
        pct = (position['total_pnl']+net-position['cost'])/position['original_cost']*100
        for key, compare in (('best_exit_net_pct', max), ('worst_exit_net_pct', min)):
            d[key] = pct if d[key] is None else compare(d[key], pct)

    def discard_exit_review(self, symbol, ts, reason):
        review = self.s.get('exit_reviews', {}).pop(symbol, None)
        if review:
            for horizon in review['remaining']:
                self.event(ts, 'POST_EXIT_DIAGNOSTIC', symbol, dict(entry_ms=review['entry_ms'],
                    exit_ms=review['exit_ms'], horizon_ms=horizon, status='MISSING', reason=reason))

    def review_after_exit(self, symbol, ts, book=None):
        """Observe later quotes, with depth and costs; no retroactive trade or NAV change."""
        review = self.s.get('exit_reviews', {}).get(symbol)
        if not review:
            return
        for horizon in list(review['remaining']):
            target = review['exit_ms']+horizon
            if ts < target:
                continue
            evidence = dict(entry_ms=review['entry_ms'], exit_ms=review['exit_ms'], horizon_ms=horizon)
            if ts > target+10000:
                evidence.update(status='MISSING', reason='NO_TIMELY_BOOK')
            elif book is not None and book['stamp'] >= target:
                _, raw, remaining = walk(book['bids'], quantity=review['qty'])
                if remaining > 1e-8:
                    evidence.update(status='MISSING', reason='INSUFFICIENT_VISIBLE_DEPTH')
                else:
                    net = raw*(1-self.policy['slip'])*(1-self.policy['fee'])
                    pct = (net/review['cost']-1)*100
                    evidence.update(status='OBSERVED_QUOTE_PROXY', hold_net_pct=pct,
                                    actual_exit_net_pct=review['exit_net_pct'],
                                    difference_pct=pct-review['exit_net_pct'])
            else:
                continue
            self.event(ts, 'POST_EXIT_DIAGNOSTIC', symbol, evidence)
            review['remaining'].remove(horizon)
        if not review['remaining']:
            del self.s['exit_reviews'][symbol]

    def execute_exit(self, symbol, ts, book):
        p = self.s['positions'][symbol]
        decision = p.get('exit')
        if not decision or book['stamp'] <= decision['ts'] or book['stamp'] <= p['last_sell_stamp']:
            return
        qty, raw, remaining = walk(book['bids'], quantity=p['qty'])
        if qty <= 0:
            return
        price = raw / qty * (1 - self.policy['slip'])
        fee = qty * price * self.policy['fee']
        proceeds = qty * price - fee
        cost = p['cost'] * qty / p['qty']
        pnl = proceeds - cost
        self.s['cash'] += proceeds
        self.s['realized'] += pnl
        p['total_pnl'] += pnl
        p['qty'] = remaining
        p['cost'] -= cost
        p['last_sell_stamp'] = book['stamp']
        self.db.execute('INSERT INTO fills(ts,symbol,side,quantity,price,fee,cash,pnl,reason,decision_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (ts, symbol, 'SELL', qty, price, fee, proceeds, pnl, decision['reason'], decision['ts']))
        if remaining <= 1e-8:
            self.s['closed'] += 1
            self.s['winning'] += p['total_pnl'] > 0
            if p.get('diagnostic'):
                self.event(ts, 'EXIT_DIAGNOSTIC', symbol, dict(p['diagnostic'],
                    exit_ms=ts, reason=decision['reason'], pnl=p['total_pnl'],
                    exit_delay_ms=ts-decision['ts'], holding_ms=ts-p['entry_ms'],
                    net_return_pct=p['total_pnl']/p['original_cost']*100))
                self.discard_exit_review(symbol, ts, 'NEXT_ROUNDTRIP')
                self.s.setdefault('exit_reviews', {})[symbol] = dict(entry_ms=p['entry_ms'],
                    exit_ms=ts, qty=p['diagnostic']['original_qty'], cost=p['original_cost'],
                    exit_net_pct=p['total_pnl']/p['original_cost']*100, remaining=[60000,180000,300000])
            del self.s['positions'][symbol]
        self.save(ts)

    def window(self, symbol, end, bars, decision_ms=None):
        p = self.s['positions'].get(symbol)
        if not p:
            return
        current = next((b for b in bars if b['t'] == end - 10000), None)
        if current and current['t'] >= p['entry_ms']:
            negative = current['buy'] * 2 - current['value'] < 0
            p['negative_windows'] = p['negative_windows'] + 1 if negative else 0
            trade = self.trades.get(symbol)
            if (p['negative_windows'] >= 2 and p['volume'] > 0 and trade
                    and 0 <= end - trade[0] <= 2000 and trade[1] < p['value'] / p['volume']):
                self.request_exit(symbol, decision_ms or end, 'NET_SELL_20S_BELOW_VWAP')
        else:
            p['negative_windows'] = 0
        if end % 60000 == 0:
            minute = [b for b in bars if end - 60000 <= b['t'] < end]
            lows = [b['l'] for b in minute if b.get('l') is not None]
            if len(minute) == 6 and end - 60000 >= p['entry_ms'] and lows:
                p['minute_lows'] = [x for x in p['minute_lows'] if x[0] >= end - 120000]
                p['minute_lows'].append([end, min(lows)])
                if len(p['minute_lows']) == 3:
                    old_stop = p['stop']
                    p['stop'] = max(old_stop, min(x[1] for x in p['minute_lows']))
                    if p['stop'] > old_stop:
                        self.event(decision_ms or end, 'PROTECTION_RAISED', symbol,
                                   dict(previous=old_stop, stop=p['stop'], minute_lows=p['minute_lows']))

    def tick(self, ts):
        for symbol in list(self.s.get('exit_reviews', {})):
            self.review_after_exit(symbol, ts)
        for symbol, order in list(self.s['pending'].items()):
            if ts > order['ts'] + self.policy['entry_wait_ms']:
                self.cancel(symbol, ts, order['last_reason'])
        if ts - self.last_checkpoint >= 10000:
            self.last_checkpoint = ts
            self.save(ts)
            report = self.report(ts)
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO daily VALUES(?,?,?)',
                                (ts // DAY, ts, json.dumps(report, allow_nan=False)))

    def report(self, ts):
        positions = self.s['positions']
        value = sum(p['qty'] * p['mark'] for p in positions.values())
        stale = sum(ts - p['mark_ms'] > 2000 for p in positions.values())
        nav = self.s['cash'] + value
        segment = self.s.get('review_segment')
        review = dict(segment, return_pct=(nav/segment['equity']-1)*100,
                      completed=self.s['closed']-segment['closed']) if segment else None
        return dict(mode='FAST_PAPER_ONLY', policy=self.policy['version'], entry_cap=self.policy['entry_cap'],
                    entries_paused=self.s.get('entries_paused',False), review_segment=review,
                    retry_unfilled=self.policy['retry_unfilled'], same_day_reentry=False, started_ms=self.s['started_ms'],
                    initial=self.policy['initial'], slots=self.policy['slots'], cash=self.s['cash'],
                    reserved=sum(x['budget'] for x in self.s['pending'].values()),
                    positions=len(positions), pending=len(self.s['pending']), equity=nav,
                    return_pct=(nav/self.policy['initial']-1)*100, realized=self.s['realized'],
                    closed=self.s['closed'], winning=self.s['winning'], stale_marks=stale,
                    uncertain_positions=sum(p['uncertain'] for p in positions.values()), asof_ms=ts)

    def close(self, ts):
        self.save(ts)
        self.db.close()
