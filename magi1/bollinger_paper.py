"""Double Bollinger + CCI PAPER account, driven only by public market data.

REST warms indicators; only a complete, continuously observed exchange-time
five-minute bar can make a new decision. No private API or historical fills.
"""
import asyncio
from collections import Counter, defaultdict
import json
import math
from statistics import fmean

from magi1.fast_bootstrap import Public, api_rows, validate
from magi1.fast_paper import Paper

FIVE = 300_000
HISTORY = 348  # 60-bar BB warmup + 288 previous widths + current width
RETAIN = 400
SETTLE = 2500  # Accepted WebSocket trade age is at most 2000 ms.
POLICY = dict(version='double-bollinger-cci-v1', initial=3000000., slots=10,
              fee=.0005, slip=.0005, entry_cap=.003, entry_wait_ms=10000,
              quote_age_ms=2000, no_new_high_ms=None, max_stop_distance=.03,
              short=20, long=60, deviations=2., cci=10,
              squeeze_lookback=288, squeeze_percentile=.2, watch_bars=6,
              turnover_multiple=1.5, turnover_lookback=20,
              entry='DOUBLE_BB_CCI_BREAKOUT', protection='SIGNAL_SIX_BAR_LOW',
              flow_exit=None, trail=None, gap_exit='FIRST_FRESH_BOOK_AFTER_GAP',
              same_day_reentry=True, retry_unfilled=False)


def features(rows):
    """Population SD; linear percentile of PRIOR 288 widths, no lookahead."""
    if len(rows) < HISTORY:
        return None
    rows = rows[-HISTORY:]
    if any(b[0] - a[0] != FIVE for a, b in zip(rows, rows[1:])):
        return None
    closes = [r[4] for r in rows]
    # Center before prefix sums to avoid subtracting two large price squares.
    origin = closes[0]
    sums, squares = [0.], [0.]
    for c in closes:
        x = c - origin
        sums.append(sums[-1] + x)
        squares.append(squares[-1] + x*x)

    def band(end, period):
        mean = (sums[end] - sums[end-period]) / period
        variance = max(0., (squares[end]-squares[end-period])/period - mean*mean)
        mid = mean + origin
        spread = 2 * math.sqrt(variance)
        return mid, mid+spread, 2*spread/mid*100

    n = len(rows)
    sm, su, sw = band(n, 20)
    _, previous_upper, previous_width = band(n-1, 20)
    lm, _, lw = band(n, 60)
    previous_middle, _, _ = band(n-1, 60)
    widths = sorted(band(end, 60)[2] for end in range(n-288, n))
    rank = (len(widths)-1) * .2
    lower = int(rank)
    threshold = widths[lower] + (widths[lower+1]-widths[lower])*(rank-lower)
    typical = [(r[2]+r[3]+r[4])/3 for r in rows[-10:]]
    avg = fmean(typical)
    deviation = fmean(abs(p-avg) for p in typical)
    cci = (typical[-1]-avg)/(.015*deviation) if deviation else 0.
    turnover = fmean(r[6] for r in rows[-21:-1])
    return dict(close=closes[-1], short_middle=sm, short_upper=su,
                short_width=sw, long_middle=lm, long_width=lw,
                squeeze_threshold=threshold, squeeze=lw <= threshold,
                breakout=closes[-2] <= previous_upper and closes[-1] > su,
                expanding=sw > previous_width,
                long_up=closes[-1] > lm and lm >= previous_middle,
                cci=cci, turnover=rows[-1][6], turnover_mean=turnover,
                volume_ok=turnover > 0 and rows[-1][6] >= 1.5*turnover,
                stop=min(r[3] for r in rows[-6:]))


class BollingerPaper(Paper):
    def __init__(self, path, stamp):
        # Initialized before Paper restores positions and calls gap().
        self.history = defaultdict(list)
        self.live = defaultdict(dict)
        self.active_since = {}
        self.watch = {}
        self.states = {}
        self.loaded = set()
        self.needs_history = set()
        self.last_cycle = stamp // FIVE * FIVE
        self.history_pages = 0
        self.history_updated = None
        self.last_processed = {}
        super().__init__(path, stamp, POLICY)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS candles(symbol TEXT,bucket INTEGER,o REAL,
            h REAL,l REAL,c REAL,volume REAL,value REAL,PRIMARY KEY(symbol,bucket));
          CREATE TABLE IF NOT EXISTS assessments(symbol TEXT PRIMARY KEY,
            bucket INTEGER,ts INTEGER,payload TEXT);
        ''')
        for s, *row in self.db.execute('SELECT * FROM candles ORDER BY symbol,bucket'):
            self.history[s].append(row)
        self.event(stamp, 'BOLLINGER_READY', '', {'policy': POLICY, 'live_orders': False})
        self.save(stamp)

    def bought_today(self, symbol, ts):
        return False  # Reentry is governed by a NEW squeeze after full exit.

    def entry_rejection(self, order, price):
        if price <= order['stop']:
            return 'PROTECTION_ALREADY_BROKEN'
        if (price-order['stop'])/price > self.policy['max_stop_distance'] + 1e-12:
            return 'STOP_DISTANCE_OVER_3_PERCENT'
        return ''

    def window(self, symbol, end, bars, decision_ms=None):
        pass  # FAST ten-second flow exits and one-minute trailing do not apply.

    def connected(self, symbols, ts):
        for symbol in symbols:
            self.invalidate_candles(symbol, ts)

    def invalidate_candles(self, symbol, ts):
        self.live.pop(symbol, None)
        self.watch.pop(symbol, None)
        self.active_since[symbol] = (ts+FIVE-1)//FIVE*FIVE
        self.loaded.discard(symbol)
        self.needs_history.add(symbol)
        self.states[symbol] = 'HISTORY_WARMUP'

    def gap(self, symbols, ts, reason):
        symbols = list(symbols)
        for symbol in symbols:
            self.invalidate_candles(symbol, ts)
        super().gap(symbols, ts, reason)

    def store_rows(self, symbol, rows):
        combined = {r[0]: r for r in self.history[symbol]}
        combined.update((r[0], r) for r in rows)
        self.history[symbol] = sorted(combined.values(), key=lambda r: r[0])[-RETAIN:]
        self.db.executemany('INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?,?)',
                            [(symbol, *r) for r in rows])
        if self.history[symbol]:
            self.db.execute('DELETE FROM candles WHERE symbol=? AND bucket<?',
                            (symbol, self.history[symbol][0][0]))

    def seed(self, symbol, rows, ts):
        rows = [validate(r) for r in rows]
        if any(r[0]+FIVE > ts for r in rows):
            raise ValueError('INCOMPLETE_WARMUP_CANDLE')
        self.store_rows(symbol, rows)
        self.loaded.add(symbol)
        self.needs_history.discard(symbol)
        # History can never arm a watch or replay a previous breakout.
        self.watch.pop(symbol, None)
        self.active_since[symbol] = max(self.active_since.get(symbol, 0), (ts+FIVE-1)//FIVE*FIVE)
        self.states[symbol] = 'LIVE_BAR_WARMUP'
        self.history_updated = ts
        self.db.commit()

    def candle_trade(self, symbol, received, price, quantity, stamp, ident):
        if not 0 <= received-stamp <= POLICY['quote_age_ms']:
            return
        bucket = stamp//FIVE*FIVE
        if bucket < self.active_since.get(symbol, received) or bucket <= self.last_processed.get(symbol, -1):
            return
        key = (stamp, int(ident))
        b = self.live[symbol].get(bucket)
        if b is None:
            b = dict(row=[bucket, price, price, price, price, 0., 0.], first=key, last=key)
            self.live[symbol][bucket] = b
        row = b['row']
        row[2], row[3] = max(row[2], price), min(row[3], price)
        row[5] += quantity
        row[6] += price*quantity
        if key < b['first']:
            row[1], b['first'] = price, key
        if key >= b['last']:
            row[4], b['last'] = price, key

    def assess(self, symbol, bucket, ts):
        rows = [r for r in self.history[symbol] if r[0] <= bucket]
        f = features(rows) if rows and rows[-1][0] == bucket else None
        if f is None:
            self.watch.pop(symbol, None)
            self.states[symbol] = 'INCOMPLETE_CANDLES'
            return
        self.states[symbol] = 'READY'
        pos = self.s['positions'].get(symbol)
        if pos:
            self.watch.pop(symbol, None)
            if f['close'] < f['short_middle'] and f['cci'] < 0:
                self.request_exit(symbol, ts, 'BB_MIDDLE_AND_CCI_NEGATIVE')
        elif symbol not in self.s['pending']:
            armed = self.watch.get(symbol)
            watching = armed is not None and 0 < bucket-armed <= 6*FIVE
            qualifies = watching and f['breakout'] and f['expanding'] and f['long_up'] and f['cci'] > 100 and f['volume_ok']
            if qualifies:
                self.signal(symbol, ts, f['close'], f['stop'], dict(f, candle_start=bucket, squeeze_start=armed))
                self.watch.pop(symbol, None)
            else:
                if not watching:
                    self.watch.pop(symbol, None)
                # Require the entire squeeze candle to start after the last exit.
                if f['squeeze'] and bucket >= self.s.get('last_exit_ms', {}).get(symbol, 0):
                    self.watch[symbol] = bucket
        self.db.execute('INSERT OR REPLACE INTO assessments VALUES(?,?,?,?)',
                        (symbol, bucket, ts, json.dumps(dict(f, watch=self.watch.get(symbol)))))

    def execute_exit(self, symbol, ts, book):
        before = symbol in self.s['positions']
        super().execute_exit(symbol, ts, book)
        if before and symbol not in self.s['positions']:
            self.watch.pop(symbol, None)
            self.s.setdefault('last_exit_ms', {})[symbol] = ts
            self.save(ts)

    def tick(self, ts):
        end = (ts-SETTLE)//FIVE*FIVE
        if end > self.last_cycle:
            self.last_cycle = end
            bucket = end-FIVE
            for symbol in sorted(self.active_since):
                b = self.live[symbol].get(bucket)
                if b and bucket >= self.active_since[symbol]:
                    self.store_rows(symbol, [b['row']])
                    if symbol in self.loaded and 0 <= ts-end <= 10000:
                        self.assess(symbol, bucket, ts)
                elif bucket >= self.active_since[symbol]:
                    self.watch.pop(symbol, None)
                    self.states[symbol] = 'INCOMPLETE_CANDLES'
                self.last_processed[symbol] = bucket
                self.live[symbol] = {t: b for t, b in self.live[symbol].items() if t > bucket}
            self.db.commit()
        if ts-self.last_checkpoint >= 10000:
            self.s['observation'] = self.observation()
        super().tick(ts)

    def observation(self):
        return dict(symbols=len(self.states), states=dict(Counter(self.states.values())),
                    watches=len(self.watch), history_pages=self.history_pages,
                    history_updated_ms=self.history_updated)

    def report(self, ts):
        return dict(super().report(ts), mode='DOUBLE_BOLLINGER_PAPER_ONLY',
                    same_day_reentry=True, observation=self.observation())


async def warm_history(paper, symbols, clock, client=None):
    """Serial bounded REST work in a thread; never block the public feeds."""
    client = client or Public()
    retries = {}
    while True:
        for symbol in symbols:
            if symbol not in paper.needs_history or clock() < retries.get(symbol, 0):
                continue
            cutoff = clock()//FIVE*FIVE
            cursor, collected = cutoff, []
            generation = paper.active_since.get(symbol)
            try:
                # Two pages provide 400 bars. Missing/no-trade bars stay missing.
                for _ in range(2):
                    payload = await asyncio.to_thread(client.candles, symbol, cursor)
                    page = api_rows(payload, cursor)
                    paper.history_pages += 1
                    collected.extend(page)
                    await asyncio.sleep(1)  # Additional REST load capped at 1 request/sec.
                    if not page:
                        break
                    cursor = min(r[0] for r in page)
                    if cursor <= cutoff-HISTORY*FIVE:
                        break
                if paper.active_since.get(symbol) == generation:
                    paper.seed(symbol, collected, clock())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                paper.states[symbol] = 'HISTORY_RETRY'
                retries[symbol] = clock()+60000
                paper.event(clock(), 'HISTORY_ERROR', symbol, {'type': type(exc).__name__})
                paper.db.commit()
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                if str(exc) == 'API_BLOCKED_STOP' or status in (418, 429):
                    await asyncio.sleep(300 if status == 418 or str(exc) == 'API_BLOCKED_STOP' else 60)
        await asyncio.sleep(1)
