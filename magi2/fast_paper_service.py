"""Independent FAST paper exit workers; public REST GETs only."""
from pathlib import Path
from threading import Event, Thread
import time

from magi2.fast_paper import PaperLedger, VENUES, QUOTES, VERSION, positive


def now():
    return time.time_ns() // 1_000_000


class PublicPaperMarket:
    def __init__(self, venue):
        # Lazy import avoids coupling the ledger and read-only reports to scanning.
        from magi2.fast_monitor import PublicMarket
        self.market = PublicMarket(venue)
        self.venue = venue

    def fx(self):
        from magi2.fast_monitor import PublicMarket
        if QUOTES[self.venue] == 'KRW':
            return 1.0, 'KRW'
        upbit = PublicMarket('upbit')
        try:
            row = upbit.get('/v1/ticker', {'markets':'KRW-USDT'})[0]
            won_per_usdt = positive(row['trade_price'])
            if self.venue == 'binance':
                return won_per_usdt, 'INITIAL_UPBIT_USDT_KRW_FIXED'
            pairs = self.market.get('/0/public/Ticker', {'pair':'USDTUSD'})['result']
            usd_per_usdt = positive(next(iter(pairs.values()))['c'][0])
            return won_per_usdt / usd_per_usdt, 'INITIAL_UPBIT_USDT_KRW_DIV_KRAKEN_USDT_USD_FIXED'
        finally:
            upbit.http.close()

    def quotes(self, symbols):
        started = now(); quotes = self.market.quotes(symbols); finished = now()
        if finished - started > 1500:
            raise ValueError('QUOTE_RESPONSE_SLOW')
        return quotes, finished

    def book(self, symbol):
        started = now()
        if self.venue in ('upbit','bithumb'):
            rows = self.market.get('/v1/orderbook', {'markets':symbol})
            row = next(r for r in rows if r['market'] == symbol)
            bids = [(x['bid_price'],x['bid_size']) for x in row['orderbook_units']]
            asks = [(x['ask_price'],x['ask_size']) for x in row['orderbook_units']]
        elif self.venue == 'binance':
            row = self.market.get('/api/v3/depth', {'symbol':symbol,'limit':100})
            bids, asks = row['bids'], row['asks']
        else:
            rows = self.market.get('/0/public/Depth', {'pair':symbol,'count':100})['result']
            row = next(iter(rows.values()))
            bids = [x[:2] for x in row['bids']]; asks = [x[:2] for x in row['asks']]
        return dict(bids=bids, asks=asks, requested_ms=started, received_ms=now())


class PaperService:
    def __init__(self, root, log, market_factory=PublicPaperMarket, clock=now):
        self.clock = clock; self.log = log; self.market_factory = market_factory
        self.ledger = PaperLedger(Path(root)/'fast_paper.sqlite3', clock())
        self.stop = Event(); self.threads = []; self.wake = {v:Event() for v in VENUES}

    def start(self):
        for venue in VENUES:
            thread = Thread(target=self.worker, args=(venue,), daemon=True, name='fast-paper-'+venue)
            thread.start(); self.threads.append(thread)
        self.log(f'fast_paper_started policy={VERSION} accounts=4 seed_krw_each=1000000 slot_krw=200000 hold_sec=300 deadline_from_signal_sec=600 live_orders=false')
        return self

    def offer(self, ident, venue, symbol, signal_ms):
        accepted = self.ledger.offer(ident, venue, symbol, signal_ms, self.clock())
        if accepted:
            self.wake[venue].set()
            self.log(f'fast_paper_signal venue={venue} symbol={symbol} status=ENTRY_PENDING')
        return accepted

    def step(self, venue, market):
        # Exit recovery runs even when there are no watches or new detections.
        self.ledger.advance(venue, self.clock())
        if self.ledger.account(venue)['funded_ms'] is None:
            fx, source = market.fx()
            self.ledger.fund(venue, fx, source, self.clock())
            self.log(f'fast_paper_funded venue={venue} seed_krw=1000000 fx_krw_per_quote={fx:.6f}')
        positions = self.ledger.active(venue)
        errors = []
        # Pending exits always precede entries and optional mark-to-market refreshes.
        exits = sorted((t for t in positions if t['status']=='EXIT_PENDING'), key=lambda t:t['deadline_ms'])
        entries = [t for t in positions if t['status']=='ENTRY_PENDING']
        for t in exits + entries:
            ready = t.get('exit_ready_ms') if t['status']=='EXIT_PENDING' else t['ready_ms']
            if self.clock() < ready:
                continue
            try:
                book = market.book(t['symbol'])
                self.ledger.advance(venue, self.clock())
                changed = (self.ledger.exit if t['status']=='EXIT_PENDING' else self.ledger.enter)(t['id'],book,self.clock())
                if changed:
                    latest = self.ledger.get(t['id'])
                    self.log(f'fast_paper_fill venue={venue} symbol={t["symbol"]} status={latest["status"]} reason={latest.get("reason")}')
            except Exception as exc:
                reason = str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                self.ledger.fail(t['id'], reason); errors.append(reason)
            # Expire a missed deadline immediately after even a failed HTTP call.
            self.ledger.advance(venue, self.clock())
        symbols = [t['symbol'] for t in self.ledger.active(venue) if t['status']=='OPEN']
        if symbols:
            try:
                quotes, received = market.quotes(symbols)
                self.ledger.mark(venue,quotes,received)
            except Exception as exc:
                errors.append(type(exc).__name__)
        self.ledger.heartbeat(venue,self.clock(),','.join(sorted(set(errors))) or None)

    def worker(self, venue):
        market = self.market_factory(venue)
        while not self.stop.is_set():
            self.wake[venue].clear()
            try:
                self.step(venue,market)
            except Exception as exc:
                self.ledger.heartbeat(venue,self.clock(),type(exc).__name__)
                self.log(f'fast_paper_error venue={venue} type={type(exc).__name__}')
            # New signals wake the worker; pending exits get priority on every pass.
            active = self.ledger.active(venue)
            delay = 1 if any(t['status'] in ('ENTRY_PENDING','EXIT_PENDING') for t in active) else 5
            self.wake[venue].wait(delay)

    def daily(self, send):
        """Persistent 09:00 KST daily delivery. send must raise if Telegram fails."""
        from magi2.fast_paper_report import summary
        ts = self.clock(); date = self.ledger.due_day(ts)
        if date:
            self.ledger.queue_daily(date, summary(self.ledger.snapshot(ts,date), daily=True), ts)
        pending = self.ledger.claim_daily(ts)
        if pending:
            date, text = pending
            send(text)
            self.ledger.daily_sent(date)
            self.log(f'fast_paper_daily_sent date={date}')
