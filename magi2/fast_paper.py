"""Durable, public-quote-only FAST paper accounts. Never submits exchange orders."""
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo
import hashlib
import json
import math
import sqlite3

KST = ZoneInfo('Asia/Seoul')
VENUES = ('upbit', 'bithumb', 'binance', 'kraken')
QUOTES = dict(upbit='KRW', bithumb='KRW', binance='USDT', kraken='USD')
VERSION = 'fast-paper-5m-10m-v1'
# Explicit simulation assumptions, not authenticated account fee quotations.
FEES_BPS = dict(upbit=5, bithumb=4, binance=10, kraken=40)
SLIPPAGE_BPS = 5
SEED_KRW = 1_000_000
SLOT_KRW = 200_000
MAX_POSITIONS = 5
HOLD_MS = 300_000
DEADLINE_MS = 600_000
ENTRY_TTL_MS = 10_000
LATENCY_MS = 250
ACTIVE = ('ENTRY_PENDING', 'OPEN', 'EXIT_PENDING')


def day(ms):
    return datetime.fromtimestamp(ms / 1000, KST).date().isoformat()


def bounds(date):
    start = datetime.fromisoformat(date).replace(tzinfo=KST)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('INVALID_POSITIVE_NUMBER')
    return value


def checked_book(book, now_ms, not_before=0):
    requested = int(book['requested_ms']); received = int(book['received_ms'])
    if not not_before <= requested <= received <= now_ms or now_ms - received > 3000 or received - requested > 1500:
        raise ValueError('STALE_OR_EARLY_BOOK')
    def available(rows):
        levels = []
        for price, size in rows:
            price = positive(price); size = float(size)
            if not math.isfinite(size) or size < 0:
                raise ValueError('INVALID_BOOK_SIZE')
            # Bithumb may include valid price levels with no remaining quantity.
            # They provide no executable liquidity and cannot set the best price.
            if size > 0:
                levels.append((price, size))
        return levels
    bids = available(book['bids'])
    asks = available(book['asks'])
    if not bids or not asks or max(p for p, _ in bids) > min(p for p, _ in asks):
        raise ValueError('EMPTY_OR_CROSSED_BOOK')
    return sorted(bids, reverse=True), sorted(asks)


def market_buy(asks, budget, fee, slip):
    remaining = budget / (1 + fee); quantity = gross = impact = 0.0
    for price, size in asks:
        # Enter only if at most 10% of the displayed ask depth can fund the slot.
        q = min(size * .10, remaining / (price * (1 + slip)))
        paid = q * price * (1 + slip)
        quantity += q; gross += paid; impact += q * price * slip; remaining -= paid
        if remaining <= budget * 1e-10:
            return quantity, gross, gross * fee, impact
    raise ValueError('INSUFFICIENT_ENTRY_DEPTH')


def market_sell(bids, quantity, fee, slip):
    remaining = quantity; sold = gross = impact = 0.0
    for price, size in bids:
        q = min(size, remaining)
        sold += q; gross += q * price * (1 - slip); impact += q * price * slip; remaining -= q
        if remaining <= quantity * 1e-10:
            break
    return sold, gross, gross * fee, impact


class PaperLedger:
    def __init__(self, path, now_ms):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS paper_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_accounts(venue TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS paper_trades(
                id TEXT PRIMARY KEY, venue TEXT NOT NULL, symbol TEXT NOT NULL, status TEXT NOT NULL,
                signal_ms INTEGER NOT NULL, entry_ms INTEGER, close_ms INTEGER, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS paper_active ON paper_trades(venue,status);
            CREATE INDEX IF NOT EXISTS paper_trade_dates ON paper_trades(signal_ms,close_ms);
            CREATE TABLE IF NOT EXISTS paper_fills(
                id INTEGER PRIMARY KEY, trade_id TEXT NOT NULL, venue TEXT NOT NULL, ts_ms INTEGER NOT NULL,
                side TEXT NOT NULL, pnl_quote REAL NOT NULL, fee_quote REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS paper_fill_dates ON paper_fills(ts_ms,venue);
            CREATE TABLE IF NOT EXISTS paper_daily(
                date TEXT PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL,
                retry_ms INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
        ''')
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO paper_meta VALUES(?,?)', ('started_ms', str(now_ms)))
            self.started_ms = int(self.db.execute('SELECT value FROM paper_meta WHERE key=?', ('started_ms',)).fetchone()[0])
            for venue in VENUES:
                account = dict(venue=venue, quote=QUOTES[venue], initial_krw=SEED_KRW,
                               fx_krw_per_quote=1.0 if QUOTES[venue] == 'KRW' else None,
                               cash_quote=float(SEED_KRW) if QUOTES[venue] == 'KRW' else None,
                               fx_source='KRW' if QUOTES[venue] == 'KRW' else None,
                               funded_ms=now_ms if QUOTES[venue] == 'KRW' else None,
                               policy=VERSION, heartbeat_ms=None, error=None)
                self.db.execute('INSERT OR IGNORE INTO paper_accounts VALUES(?,?)', (venue, self._json(account)))

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))

    def _account(self, venue):
        row = self.db.execute('SELECT payload FROM paper_accounts WHERE venue=?', (venue,)).fetchone()
        if not row:
            raise ValueError('UNKNOWN_VENUE')
        return json.loads(row[0])

    def _save_account(self, account):
        self.db.execute('UPDATE paper_accounts SET payload=? WHERE venue=?', (self._json(account), account['venue']))

    def _trade(self, ident):
        row = self.db.execute('SELECT payload FROM paper_trades WHERE id=?', (ident,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, trade):
        self.db.execute('''INSERT OR REPLACE INTO paper_trades VALUES(?,?,?,?,?,?,?,?)''',
                        (trade['id'], trade['venue'], trade['symbol'], trade['status'], trade['signal_ms'],
                         trade.get('entry_ms'), trade.get('close_ms'), self._json(trade)))

    def _active(self, venue):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM paper_trades WHERE venue=? AND status IN ('ENTRY_PENDING','OPEN','EXIT_PENDING') ORDER BY signal_ms", (venue,))]

    def account(self, venue):
        with self.lock:
            return self._account(venue)

    def get(self, ident):
        with self.lock:
            return self._trade(ident)

    def active(self, venue):
        with self.lock:
            return self._active(venue)

    def fund(self, venue, fx, source, now_ms):
        fx = positive(fx)
        with self.lock, self.db:
            account = self._account(venue)
            if account['funded_ms'] is not None:
                return False
            account.update(fx_krw_per_quote=fx, cash_quote=SEED_KRW / fx,
                           fx_source=source, funded_ms=now_ms)
            self._save_account(account)
            return True

    def heartbeat(self, venue, now_ms, error=None):
        with self.lock, self.db:
            account = self._account(venue)
            account.update(heartbeat_ms=now_ms, error=error)
            self._save_account(account)

    def offer(self, ident, venue, symbol, signal_ms, now_ms, *, strategy_version='fast-buy-flow-v1'):
        """Reserve one slot for a NEW strong signal, before notification throttling."""
        with self.lock, self.db:
            if self._trade(ident):
                return False
            account = self._account(venue); active = self._active(venue)
            reason = None; budget = 0.0
            if not self.started_ms <= signal_ms <= now_ms or now_ms > signal_ms + ENTRY_TTL_MS:
                reason = 'STALE_SIGNAL'
            elif account['funded_ms'] is None:
                reason = 'FX_NOT_READY'
            elif any(t['symbol'] == symbol for t in active):
                reason = 'SYMBOL_ALREADY_OPEN'
            elif any(now_ms >= t['deadline_ms'] for t in active):
                reason = 'OVERDUE_EXIT'
            elif len(active) >= MAX_POSITIONS:
                reason = 'ALL_SLOTS_USED'
            elif self.db.execute('SELECT 1 FROM paper_trades WHERE venue=? AND symbol=? AND close_ms>? LIMIT 1',
                                 (venue, symbol, now_ms - 60_000)).fetchone():
                reason = 'COOLDOWN'
            else:
                budget = SLOT_KRW / account['fx_krw_per_quote']
                reserved = sum(t['budget_quote'] for t in active if t['status'] == 'ENTRY_PENDING')
                if account['cash_quote'] - reserved < budget - 1e-9:
                    reason = 'INSUFFICIENT_CASH'
            trade = dict(id=ident, venue=venue, symbol=symbol, signal_ms=signal_ms,
                         strategy_version=strategy_version,
                         entry_spread_limit_bps=None if strategy_version=='fast-volume-accel-v2' else 15,
                         status='SKIPPED' if reason else 'ENTRY_PENDING', reason=reason,
                         deadline_ms=signal_ms + DEADLINE_MS, ready_ms=now_ms + LATENCY_MS,
                         budget_quote=budget, policy=VERSION, fee_bps=FEES_BPS[venue],
                         slippage_bps=SLIPPAGE_BPS, remaining_qty=0.0, remaining_cost=0.0,
                         realized_quote=0.0, exit_proceeds=0.0, last_book_hash=None,
                         last_error=None, last_bid=None, mark_ms=None)
            self._save(trade)
            return reason is None

    def advance(self, venue, now_ms):
        """Deadlines survive watch expiry, quote errors and process restarts."""
        with self.lock, self.db:
            for t in self._active(venue):
                if t['status'] == 'ENTRY_PENDING' and now_ms > t['signal_ms'] + ENTRY_TTL_MS:
                    t.update(status='SKIPPED', reason='ENTRY_QUOTE_TIMEOUT')
                elif t['status'] != 'ENTRY_PENDING':
                    if now_ms >= t['deadline_ms']:
                        t.update(status='EXIT_PENDING', reason='DEADLINE_10M',
                                 exit_ready_ms=min(t.get('exit_ready_ms', now_ms), now_ms),
                                 deadline_missed=True)
                    elif now_ms >= t['exit_due_ms'] and t['status'] == 'OPEN':
                        t.update(status='EXIT_PENDING', reason='HOLD_5M', exit_ready_ms=now_ms + LATENCY_MS)
                self._save(t)

    def fail(self, ident, reason):
        with self.lock, self.db:
            t = self._trade(ident)
            if t and t['status'] in ACTIVE:
                t['last_error'] = str(reason)[:100]
                self._save(t)

    def mark(self, venue, quotes, now_ms):
        with self.lock, self.db:
            for t in self._active(venue):
                if t['status'] == 'ENTRY_PENDING' or t['symbol'] not in quotes:
                    continue
                bid, ask = quotes[t['symbol']]
                if not all(math.isfinite(x) and x > 0 for x in (bid, ask)) or ask < bid:
                    continue
                t.update(last_bid=bid, mark_ms=now_ms)
                self._save(t)

    def _fill(self, t, side, ts, qty, gross, fee, impact, pnl, book):
        fill = dict(mode='PAPER', quantity=qty, gross_quote=gross, fee_quote=fee,
                    slippage_quote=impact, average_price=gross / qty,
                    quote_requested_ms=book['requested_ms'], quote_received_ms=book['received_ms'],
                    reason=t.get('reason'), policy=t['policy'])
        self.db.execute('INSERT INTO paper_fills(trade_id,venue,ts_ms,side,pnl_quote,fee_quote,payload) VALUES(?,?,?,?,?,?,?)',
                        (t['id'], t['venue'], ts, side, pnl, fee, self._json(fill)))

    def enter(self, ident, book, now_ms):
        with self.lock, self.db:
            t = self._trade(ident)
            if not t or t['status'] != 'ENTRY_PENDING':
                return False
            if now_ms > t['signal_ms'] + ENTRY_TTL_MS:
                t.update(status='SKIPPED', reason='ENTRY_QUOTE_TIMEOUT'); self._save(t); return False
            if any(x['status'] != 'ENTRY_PENDING' and now_ms >= x['deadline_ms'] for x in self._active(t['venue'])):
                t.update(status='SKIPPED', reason='OVERDUE_EXIT'); self._save(t); return False
            bids, asks = checked_book(book, now_ms, t['ready_ms'])
            spread_limit=t.get('entry_spread_limit_bps',15)
            if spread_limit is not None and (asks[0][0] / bids[0][0] - 1) * 10000 > spread_limit:
                t.update(status='SKIPPED', reason='ENTRY_SPREAD'); self._save(t); return False
            try:
                qty, gross, fee, impact = market_buy(asks, t['budget_quote'], t['fee_bps'] / 10000, t['slippage_bps'] / 10000)
            except ValueError as exc:
                t.update(status='SKIPPED', reason=str(exc)); self._save(t); return False
            cost = gross + fee; account = self._account(t['venue'])
            if cost > account['cash_quote'] + 1e-8:
                raise ValueError('CASH_INVARIANT')
            account['cash_quote'] = max(0.0, account['cash_quote'] - cost)
            t.update(status='OPEN', entry_ms=book['received_ms'], entry_qty=qty,
                     entry_cost=cost, remaining_qty=qty, remaining_cost=cost,
                     exit_due_ms=min(book['received_ms'] + HOLD_MS, t['deadline_ms']),
                     last_bid=bids[0][0], mark_ms=book['received_ms'], last_error=None)
            self._fill(t, 'BUY', book['received_ms'], qty, gross, fee, impact, 0, book)
            self._save(t); self._save_account(account)
            return True

    def exit(self, ident, book, now_ms):
        with self.lock, self.db:
            t = self._trade(ident)
            if not t or t['status'] != 'EXIT_PENDING':
                return False
            bids, _ = checked_book(book, now_ms, t['exit_ready_ms'])
            fingerprint = hashlib.sha256(self._json(bids).encode()).hexdigest()
            # Do not repeatedly consume the same unchanged displayed liquidity.
            if fingerprint == t['last_book_hash']:
                return False
            qty, gross, fee, impact = market_sell(bids, t['remaining_qty'], t['fee_bps'] / 10000, t['slippage_bps'] / 10000)
            basis = t['remaining_cost'] * qty / t['remaining_qty']; proceeds = gross - fee
            account = self._account(t['venue']); account['cash_quote'] += proceeds
            t['realized_quote'] += proceeds - basis; t['exit_proceeds'] += proceeds
            t['remaining_qty'] -= qty; t['remaining_cost'] -= basis
            t.update(last_book_hash=fingerprint, last_bid=bids[0][0], mark_ms=book['received_ms'], last_error=None)
            self._fill(t, 'SELL', book['received_ms'], qty, gross, fee, impact, proceeds - basis, book)
            if t['remaining_qty'] <= t['entry_qty'] * 1e-10:
                t.update(status='CLOSED', remaining_qty=0.0, remaining_cost=0.0, close_ms=book['received_ms'],
                         deadline_delay_ms=max(0, book['received_ms'] - t['deadline_ms']))
            else:
                t['last_error'] = 'PARTIAL_EXIT_DEPTH'
            self._save(t); self._save_account(account)
            return True

    def snapshot(self, now_ms, date=None):
        date = date or day(now_ms); start, end = bounds(date)
        with self.lock:
            output = []
            for venue in VENUES:
                a = self._account(venue); fx = a['fx_krw_per_quote']; active = self._active(venue)
                held = [t for t in active if t['remaining_qty'] > 0]
                unknown = [t for t in held if t['mark_ms'] is None or now_ms - t['mark_ms'] > 15_000]
                value = sum(t['remaining_qty'] * t['last_bid'] * (1-t['slippage_bps']/10000) * (1-t['fee_bps']/10000)
                            for t in held if t['last_bid'] is not None)
                equity = (a['cash_quote'] + value) * fx if fx and not unknown else None
                pnl, fees = self.db.execute('SELECT COALESCE(SUM(pnl_quote),0),COALESCE(SUM(fee_quote),0) FROM paper_fills WHERE venue=? AND ts_ms>=? AND ts_ms<?', (venue,start,end)).fetchone()
                closed = [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM paper_trades WHERE venue=? AND status='CLOSED' AND close_ms>=? AND close_ms<?", (venue,start,end))]
                buys = self.db.execute("SELECT COUNT(*) FROM paper_fills WHERE venue=? AND side='BUY' AND ts_ms>=? AND ts_ms<?",(venue,start,end)).fetchone()[0]
                skipped = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE venue=? AND status='SKIPPED' AND signal_ms>=? AND signal_ms<?",(venue,start,end)).fetchone()[0]
                cohorts={}
                for t in closed:
                    version=t.get('execution_version') or t.get('strategy_version','fast-buy-flow-v1')
                    c=cohorts.setdefault(version,{'closed':0,'closed_trade_pnl_krw':0.,'cycles':0,
                        'gross_krw':0.,'fees_krw':0.,'slippage_krw':0.,'forced_pnl_krw':0.})
                    c['closed']+=1;c['closed_trade_pnl_krw']+=t['realized_quote']*(fx or 0)
                    c['cycles']+=t.get('cycles_completed',0)
                    for target,source in [('gross_krw','gross_pnl'),('fees_krw','fees_paid'),
                                          ('slippage_krw','slippage_paid'),('forced_pnl_krw','forced_exit_pnl')]:
                        c[target]+=t.get(source,0)*(fx or 0)
                output.append(dict(a, cash_krw=a['cash_quote']*fx if fx else None, equity_krw=equity,cohorts=cohorts,
                                   active=active, unknown_marks=len(unknown), pnl_krw=pnl*fx if fx else None,
                                   fees_krw=fees*fx if fx else None, bought=buys, closed=len(closed),
                                   wins=sum(t['realized_quote']>0 for t in closed), skipped=skipped,
                                   late_closed=sum(t.get('deadline_delay_ms',0)>0 for t in closed),
                                   overdue=sum(now_ms>t['deadline_ms'] for t in held)))
            return dict(date=date, now_ms=now_ms, started_ms=self.started_ms, accounts=output)

    def history(self, now_ms, limit=10):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                'SELECT payload FROM paper_trades WHERE entry_ms IS NOT NULL AND entry_ms<=? ORDER BY entry_ms DESC,id DESC LIMIT ?', (now_ms,limit))]

    def due_day(self, now_ms):
        clock = datetime.fromtimestamp(now_ms / 1000, KST)
        if clock.hour < 9:
            return None
        latest = clock.date() - timedelta(days=1)
        with self.lock:
            last = self.db.execute('SELECT MAX(date) FROM paper_daily').fetchone()[0]
        candidate = datetime.fromisoformat(last).date() + timedelta(days=1) if last else datetime.fromisoformat(day(self.started_ms)).date()
        return candidate.isoformat() if candidate <= latest else None

    def queue_daily(self, date, body, now_ms):
        if len(body) > 3500:
            raise ValueError('DAILY_MESSAGE_TOO_LONG')
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO paper_daily(date,body,status,retry_ms) VALUES(?,?,?,?)', (date,body,'PENDING',now_ms))

    def claim_daily(self, now_ms):
        with self.lock, self.db:
            row = self.db.execute("SELECT date,body FROM paper_daily WHERE status!='SENT' AND retry_ms<=? ORDER BY date LIMIT 1",(now_ms,)).fetchone()
            if row:
                self.db.execute("UPDATE paper_daily SET status='SENDING',retry_ms=?,attempts=attempts+1 WHERE date=?", (now_ms+120_000,row[0]))
            return row

    def daily_sent(self, date):
        with self.lock, self.db:
            self.db.execute("UPDATE paper_daily SET status='SENT' WHERE date=?",(date,))

    def close(self):
        with self.lock:
            self.db.close()
