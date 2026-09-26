"""Causal FAST candidate-v1 PAPER ledger. Public quotes only; no order client.

The signal observer owns this object and its SQLite connection on one thread.
Account and fill changes commit atomically; never replay historical captures.
"""
import json
from pathlib import Path
import sqlite3

DAY = 86_400_000
POLICY = dict(version='fast-flow-paper-v1', initial=3000000., slots=10,
              fee=.0005, slip=.0005, entry_cap=.005, entry_wait_ms=10000,
              quote_age_ms=2000, no_new_high_ms=180000,
              entry='FLOW_CONFIRMATION_A', protection='PRIOR_60S_LOW',
              flow_exit='TWO_10S_NET_SELL_AND_BELOW_ENTRY_VWAP',
              trail='THREE_COMPLETED_MINUTE_LOWS_RATCHET',
              gap_exit='FIRST_FRESH_BOOK_AFTER_GAP', same_day_reentry=False)


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
    def __init__(self, path, stamp):
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
        ''')
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(
            policy=POLICY, started_ms=stamp, updated_ms=stamp, cash=POLICY['initial'],
            realized=0., positions={}, pending={}, closed=0, winning=0)
        if self.s['policy'] != POLICY:
            raise ValueError('FAST_PAPER_POLICY_MISMATCH')
        self.books = {}
        self.trades = {}
        self.last_checkpoint = stamp
        if row:
            # No invented prices or continuity across process downtime.
            self.gap(set(self.s['pending']) | set(self.s['positions']), stamp, 'RESTART')
        self.event(stamp, 'START', '', dict(policy=POLICY, restored=bool(row)))
        self.save(stamp)

    def event(self, ts, kind, symbol, payload):
        self.db.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                        (ts, kind, symbol, json.dumps(payload, allow_nan=False)))

    def save(self, ts):
        self.s['updated_ms'] = ts
        if self.s['cash'] < -1e-6 or len(self.s['positions']) + len(self.s['pending']) > POLICY['slots']:
            raise RuntimeError('FAST_PAPER_CAPITAL_INVARIANT')
        reserved = sum(x['budget'] for x in self.s['pending'].values())
        if reserved > self.s['cash'] + 1e-6:
            raise RuntimeError('FAST_PAPER_RESERVATION_INVARIANT')
        self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)',
                        (json.dumps(self.s, allow_nan=False),))
        self.db.commit()

    def signal(self, symbol, ts, price, low, features):
        if ts < self.s['started_ms']:
            return
        day = ts // DAY
        if self.db.execute('SELECT 1 FROM signals WHERE symbol=? AND day=?', (symbol, day)).fetchone():
            return
        positions, pending = self.s['positions'], self.s['pending']
        vacant = POLICY['slots'] - len(positions) - len(pending)
        free = self.s['cash'] - sum(x['budget'] for x in pending.values())
        budget = free / vacant if vacant > 0 else 0.
        reason = ('ALREADY_HELD' if symbol in positions or symbol in pending else
                  'NO_SLOT' if vacant <= 0 else 'INSUFFICIENT_CASH' if budget < 5000 else
                  'INVALID_PROTECTION' if low is None or low <= 0 or low >= price else '')
        self.db.execute('INSERT INTO signals VALUES(?,?,?,?,?,?)',
                        (symbol, day, ts, 'SKIPPED' if reason else 'PENDING', reason,
                         json.dumps(dict(features, reference=price, protection=low, budget=budget))))
        if not reason:
            pending[symbol] = dict(ts=ts, day=day, reference=price, stop=low,
                                   budget=budget, last_reason='WAIT_FRESH_BOOK')
        self.save(ts)

    def cancel(self, symbol, ts, reason):
        order = self.s['pending'].pop(symbol, None)
        if order:
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
            self.books.pop(symbol, None)
            self.trades.pop(symbol, None)
            self.cancel(symbol, ts, 'DATA_GAP_' + reason)
            p = self.s['positions'].get(symbol)
            if p:
                p['uncertain'] = True
                self.request_exit(symbol, ts, 'DATA_GAP_' + reason)
        self.save(ts)

    def on_trade(self, symbol, ts, price, quantity, stamp):
        if not 0 <= ts - stamp <= POLICY['quote_age_ms']:
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
        if not 0 <= ts - book['stamp'] <= POLICY['quote_age_ms']:
            return
        self.books[symbol] = book
        p = self.s['positions'].get(symbol)
        if p:
            p['mark'] = book['bp'] * (1 - POLICY['slip']) * (1 - POLICY['fee'])
            p['mark_ms'] = ts
            if book['bp'] <= p['stop']:
                self.request_exit(symbol, ts, 'PROTECTION')
            if not p.get('exit') and ts - p['peak_ms'] >= POLICY['no_new_high_ms']:
                if p['qty'] * p['mark'] <= p['cost']:
                    self.request_exit(symbol, ts, 'NO_NEW_HIGH_3M_NONPOSITIVE')
            self.execute_exit(symbol, ts, book)
        order = self.s['pending'].get(symbol)
        if not order:
            return
        if ts > order['ts'] + POLICY['entry_wait_ms']:
            self.cancel(symbol, ts, order['last_reason'])
            return
        # Both receive time and exchange timestamp must follow the decision.
        trade = self.trades.get(symbol)
        if ts <= order['ts'] or book['stamp'] <= order['ts'] or not trade or not 0 <= ts - trade[0] <= 2000:
            return
        if book['bp'] <= order['stop']:
            self.cancel(symbol, ts, 'PROTECTION_ALREADY_BROKEN')
            return
        raw_budget = order['budget'] / ((1 + POLICY['fee']) * (1 + POLICY['slip']))
        qty, raw, remaining = walk(book['asks'], budget=raw_budget)
        if remaining > 1e-6 or qty <= 0:
            order['last_reason'] = 'INSUFFICIENT_VISIBLE_DEPTH'
            return
        price = raw / qty * (1 + POLICY['slip'])
        if price > order['reference'] * (1 + POLICY['entry_cap']):
            order['last_reason'] = 'ENTRY_PRICE_CAP'
            return
        cost = order['budget']
        self.s['cash'] -= cost
        self.s['positions'][symbol] = dict(qty=qty, cost=cost, original_cost=cost,
            entry_ms=ts, entry_price=price, stop=order['stop'], peak=trade[1], peak_ms=ts,
            value=0., volume=0., negative_windows=0, minute_lows=[], exit=None,
            mark=book['bp'] * (1-POLICY['slip']) * (1-POLICY['fee']), mark_ms=ts,
            uncertain=False, total_pnl=0., last_sell_stamp=-1)
        self.db.execute('INSERT INTO fills(ts,symbol,side,quantity,price,fee,cash,pnl,reason,decision_ms) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (ts, symbol, 'BUY', qty, price, qty*price*POLICY['fee'], -cost, 0., 'FLOW_CONFIRMATION_A', order['ts']))
        self.db.execute("UPDATE signals SET status='BOUGHT',reason='' WHERE symbol=? AND day=?", (symbol, order['day']))
        del self.s['pending'][symbol]
        self.save(ts)

    def execute_exit(self, symbol, ts, book):
        p = self.s['positions'][symbol]
        decision = p.get('exit')
        if not decision or book['stamp'] <= decision['ts'] or book['stamp'] <= p['last_sell_stamp']:
            return
        qty, raw, remaining = walk(book['bids'], quantity=p['qty'])
        if qty <= 0:
            return
        price = raw / qty * (1 - POLICY['slip'])
        fee = qty * price * POLICY['fee']
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
        for symbol, order in list(self.s['pending'].items()):
            if ts > order['ts'] + POLICY['entry_wait_ms']:
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
        return dict(mode='FAST_PAPER_ONLY', policy=POLICY['version'], started_ms=self.s['started_ms'],
                    initial=POLICY['initial'], slots=POLICY['slots'], cash=self.s['cash'],
                    reserved=sum(x['budget'] for x in self.s['pending'].values()),
                    positions=len(positions), pending=len(self.s['pending']), equity=nav,
                    return_pct=(nav/POLICY['initial']-1)*100, realized=self.s['realized'],
                    closed=self.s['closed'], winning=self.s['winning'], stale_marks=stale,
                    uncertain_positions=sum(p['uncertain'] for p in positions.values()), asof_ms=ts)

    def close(self, ts):
        self.save(ts)
        self.db.close()
