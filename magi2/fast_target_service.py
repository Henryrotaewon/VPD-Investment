"""Public-data paper target/stop workers, isolated per venue."""
from pathlib import Path
from threading import Event, Thread
from magi2.fast_paper import VENUES
from magi2.fast_paper_service import PaperService, now
from magi2.fast_tick_service import TickMarket
from magi2.fast_target_paper import TargetLedger, VERSION


class TargetPaperService(PaperService):
    def __init__(self, root, log, market_factory=TickMarket, clock=now):
        self.clock=clock; self.log=log; self.market_factory=market_factory
        self.ledger=TargetLedger(Path(root)/'fast_paper.sqlite3', clock())
        self.stop=Event(); self.threads=[]; self.wake={v:Event() for v in VENUES}

    def start(self):
        for venue in VENUES:
            thread=Thread(target=self.worker, args=(venue,), daemon=True, name='fast-target-'+venue)
            thread.start(); self.threads.append(thread)
        self.log(f'fast_paper_started policy={VERSION} take_profit_pct=12 stop_loss_pct=-6 '
                 'deadline=none reentry=sold_day_kst live_orders=false')
        return self

    def clear(self):
        result=self.ledger.liquidate_all(self.clock())
        for wake in self.wake.values(): wake.set()
        return result

    def step(self, venue, market):
        self.ledger.advance(venue, self.clock())
        if self.ledger.account(venue)['funded_ms'] is None:
            fx, source=market.fx(); self.ledger.fund(venue, fx, source, self.clock())
        errors=[]
        positions=sorted(self.ledger.active(venue), key=lambda t: {'EXIT_PENDING':0,'OPEN':1,'ENTRY_PENDING':2}[t['status']])
        for snapshot in positions:
            ident=snapshot['id']
            try:
                t=self.ledger.get(ident)
                if t['status']=='ENTRY_PENDING' and self.clock()<t['ready_ms']: continue
                book=market.book(t['symbol'])
                if t['status']=='ENTRY_PENDING':
                    self.ledger.enter_market(ident, book, market.rules(t['symbol']), self.clock())
                    continue
                if t['status']=='OPEN': self.ledger.check_stop(ident, book, self.clock())
                t=self.ledger.get(ident)
                if t['status']=='EXIT_PENDING':
                    if book['requested_ms']<t['exit_ready_ms']: book=market.book(t['symbol'])
                    self.ledger.exit(ident, book, self.clock())
                elif t['status']=='OPEN':
                    # Stop and manual exit checks do not depend on trade-tape availability.
                    order=t['order']
                    if order and order['active_ms'] is None and order['price']>max(float(p) for p,q in book['asks']):
                        continue  # Limit is waiting outside public depth; never invent its queue.
                    tape=market.tape(t['symbol'])
                    self.ledger.limit_step(ident, tape, book, t['rules'], self.clock())
            except Exception as exc:
                reason=str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self.ledger.fail(ident, reason); errors.append(reason)
                self.log(f'fast_target_error venue={venue} symbol={snapshot["symbol"]} reason={reason[:80]}')
        self.ledger.heartbeat(venue, self.clock(), ','.join(sorted(set(errors))) or None)

    def worker(self, venue):
        market=self.market_factory(venue)
        while not self.stop.is_set():
            self.wake[venue].clear()
            try: self.step(venue, market)
            except Exception as exc:
                self.ledger.heartbeat(venue, self.clock(), type(exc).__name__)
                self.log(f'fast_target_worker_error venue={venue} type={type(exc).__name__}')
            self.wake[venue].wait(1)
