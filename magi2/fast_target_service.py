"""Public-data paper target/stop workers, isolated per venue."""
from pathlib import Path
import json
from threading import Event, Thread
from magi2.fast_paper import VENUES, checked_book
from magi2.fast_paper_service import PaperService, now
from magi2.fast_tick_service import TickMarket
from magi2.fast_target_paper import TargetLedger, VERSION


class TargetPaperService(PaperService):
    def __init__(self, root, log, market_factory=TickMarket, clock=now):
        self.clock=clock; self.log=log; self.market_factory=market_factory
        self.ledger=TargetLedger(Path(root)/'fast_paper.sqlite3', clock())
        self.stop=Event(); self.threads=[]; self.wake={v:Event() for v in VENUES}

    def start(self):
        with self.ledger.lock:
            count=self.ledger.db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]
        self.log(f'fast_session_ready boundary=0730_KST trades={count} enabled={self.ledger.control()["enabled"]} '
                 'initial_krw_total=4000000 slots_each=5 max_slot_krw=200000')
        self.log_restored_state()
        for venue in VENUES:
            thread=Thread(target=self.worker, args=(venue,), daemon=True, name='fast-target-'+venue)
            thread.start(); self.threads.append(thread)
        thread=Thread(target=self.probe_entry_books,daemon=True,name='fast-book-recheck')
        thread.start();self.threads.append(thread)
        self.log(f'fast_paper_started policy={VERSION} take_profit_pct=12 stop_loss_pct=-6 '
                 'deadline=none reentry=sold_day_kst live_orders=false')
        return self

    def log_trade(self, ident, phase):
        """Bounded accounting diagnostics, without dumping raw ledger payloads."""
        with self.ledger.lock:
            t=self.ledger.get(ident)
            if not t:return
            account=self.ledger.account(t['venue']);fx=account['fx_krw_per_quote']
            data={key:t.get(key) for key in ('id','venue','symbol','status','signal_ms','entry_ms','close_ms',
                  'reason','buy_average','entry_bid','entry_ask','stop_price','last_bid','remaining_qty')}
            data.update(phase=phase,quote=account['quote'],error=(t.get('last_error') or '')[:80])
            for key in ('entry_cost','exit_proceeds','realized_quote','fees_paid'):
                data[key+'_krw']=t.get(key,0)*fx if fx else None
            data['net_pct']=t['realized_quote']/t['entry_cost']*100 if t['entry_cost'] and t['status']=='CLOSED' else None
            gross,qty=self.ledger.db.execute("SELECT COALESCE(SUM(json_extract(payload,'$.gross_quote')),0), "
                "COALESCE(SUM(json_extract(payload,'$.quantity')),0) FROM paper_fills WHERE trade_id=? AND side='SELL'",
                (ident,)).fetchone()
            data['sell_average']=gross/qty if qty else None
        self.log('fast_paper_state '+json.dumps(data,ensure_ascii=False,separators=(',',':')))

    def log_restored_state(self):
        with self.ledger.lock:
            ids=[r[0] for r in self.ledger.db.execute(
                'SELECT id FROM paper_trades ORDER BY signal_ms DESC,id DESC LIMIT 10')]
            for venue in VENUES:
                a=self.ledger.account(venue);fx=a['fx_krw_per_quote']
                cash=a['cash_quote']*fx if fx else None
                self.log(f'fast_paper_account venue={venue} cash_krw={cash}')
            for ident in ids:self.log_trade(ident,'restored')

    def probe_entry_books(self):
        # Independent sessions keep diagnostic HTTP calls out of the exit workers.
        for venue in VENUES:
            if self.stop.is_set():return
            market=None
            try:
                market=self.market_factory(venue)
                self.probe_recent_entry_books(venue,market)
            except Exception as exc:
                self.log(f'fast_book_recheck venue={venue} status=UNAVAILABLE reason={type(exc).__name__}')
            finally:
                if market is not None:market.market.http.close()

    def probe_recent_entry_books(self, venue, market):
        """Recheck public books for recent validation failures, without replaying buys."""
        ts=self.clock();start,_=self.ledger.report_bounds(self.ledger.report_day(ts))
        with self.ledger.lock:
            symbols=[r[0] for r in self.ledger.db.execute(
                "SELECT symbol FROM paper_trades WHERE venue=? AND status='SKIPPED' AND signal_ms>=? "
                "AND json_extract(payload,'$.last_error') LIKE 'INVALID_POSITIVE_NUMBER%' "
                "GROUP BY symbol ORDER BY MAX(signal_ms) DESC LIMIT 3", (venue,start))]
        for symbol in symbols:
            try:
                book=market.book(symbol)
                bids,asks=checked_book(book,self.clock())
                padded={side:sum(float(p)==0 and float(q)==0 for p,q in book[side]) for side in ('bids','asks')}
                self.log(f'fast_book_recheck venue={venue} symbol={symbol} status=VALID '
                         f'zero_padding_bids={padded["bids"]} zero_padding_asks={padded["asks"]} '
                         f'best_bid={bids[0][0]} best_ask={asks[0][0]} replay_entry=false')
            except Exception as exc:
                reason=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                self.log(f'fast_book_recheck venue={venue} symbol={symbol} status=UNAVAILABLE reason={reason[:80]} replay_entry=false')

    def clear(self):
        result=self.ledger.pause_and_clear(self.clock())
        for wake in self.wake.values(): wake.set()
        self.log('fast_control paused=true liquidation_requested=true')
        return result

    def resume(self):
        accepted=self.ledger.resume(self.clock())
        if accepted:
            for wake in self.wake.values(): wake.set()
        self.log(f'fast_control resume_accepted={accepted}')
        return accepted

    def step(self, venue, market):
        for ident in self.ledger.advance(venue, self.clock()):self.log_trade(ident,'entry_expired')
        if self.ledger.account(venue)['funded_ms'] is None:
            fx, source=market.fx(); self.ledger.fund(venue, fx, source, self.clock())
            self.log(f'fast_paper_funded venue={venue} seed_krw=1000000')
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
            finally:
                current=self.ledger.get(ident)
                if current and any(current.get(k)!=snapshot.get(k) for k in ('status','entry_cost','exit_proceeds')):
                    self.log_trade(ident,'transition')
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
