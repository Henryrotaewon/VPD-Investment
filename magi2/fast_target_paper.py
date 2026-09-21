"""FAST paper v5: one entry, fixed +12% limit, -6% market stop; no time exit."""
from decimal import ROUND_CEILING
import json
from magi2.fast_paper import VENUES, day, bounds, checked_book, market_buy, ENTRY_TTL_MS
from magi2.fast_tick_paper import TickLedger
from magi2.fast_tick_rules import dec, tick_at, floor_qty, valid_size

VERSION = 'fast-target-v5'


def target_price(average, rules):
    price = dec(average)*dec('1.12')
    # Rounding up may cross into a different exchange price band.
    for _ in range(10):
        tick = tick_at(price, rules)
        rounded = (price/tick).to_integral_value(rounding=ROUND_CEILING)*tick
        if rounded == price:
            if price < dec(rules.get('min_price', 0)) or (rules.get('max_price') and price > dec(rules['max_price'])):
                raise ValueError('PRICE_OUTSIDE_LIMITS')
            return float(price)
        price = rounded
    raise ValueError('TARGET_TICK_ROUNDING')


class TargetLedger(TickLedger):
    execution_version = VERSION

    def result_rows(self, now_ms):
        """Keep every current position ahead of the ten most recent completed buys."""
        with self.lock:
            active = self.db.execute("SELECT payload FROM paper_trades WHERE signal_ms<=? "
                "AND status IN ('ENTRY_PENDING','OPEN','EXIT_PENDING') ORDER BY signal_ms DESC,id DESC",
                (now_ms,)).fetchall()
            closed = self.db.execute("SELECT payload FROM paper_trades WHERE close_ms<=? "
                "AND status='CLOSED' AND entry_ms IS NOT NULL ORDER BY close_ms DESC,id DESC LIMIT 10",
                (now_ms,)).fetchall()
            return [json.loads(r[0]) for r in active + closed]

    def offer(self, ident, venue, symbol, signal_ms, now_ms, **kwargs):
        with self.lock, self.db:
            if self._trade(ident): return False
            accepted = super().offer(ident, venue, symbol, signal_ms, now_ms, **kwargs)
            t = self._trade(ident)
            start, end = bounds(day(now_ms))
            sold = self.db.execute('SELECT 1 FROM paper_fills f JOIN paper_trades t ON t.id=f.trade_id '
                "WHERE t.venue=? AND t.symbol=? AND f.side='SELL' AND f.ts_ms>=? AND f.ts_ms<? LIMIT 1",
                (venue, symbol, start, end)).fetchone()
            if sold:
                t.update(status='SKIPPED', reason='SOLD_TODAY_KST', session_cash=0.)
                self._event(t, now_ms, 'ENTRY_BLOCKED', reason='SOLD_TODAY_KST')
                accepted = False
            t.update(deadline_ms=None, take_profit_pct=12., stop_loss_pct=-6.)
            self._save(t)
            return accepted

    def advance(self, venue, now_ms):
        with self.lock, self.db:
            for t in self._active(venue):
                if t['status']=='ENTRY_PENDING' and now_ms>t['signal_ms']+ENTRY_TTL_MS:
                    t.update(status='SKIPPED', reason='ENTRY_QUOTE_TIMEOUT', session_cash=0., close_ms=now_ms)
                    self._save(t)

    def sell_price(self, t, last, rules):
        return t['take_profit_price']

    def enter_market(self, ident, book, rules, now_ms):
        with self.lock, self.db:
            t = self._trade(ident)
            if not t or t['status']!='ENTRY_PENDING': return False
            if now_ms>t['signal_ms']+ENTRY_TTL_MS:
                self.advance(t['venue'], now_ms); return False
            bids, asks = checked_book(book, now_ms, t['ready_ms'])
            qty, gross, _, slip = market_buy(asks, t['budget_quote'], t['fee_bps']/10000, t['slippage_bps']/10000)
            rounded = floor_qty(qty, rules.get('market_step', rules['step']))
            average = gross/qty
            if not valid_size(average, rounded, rules, market=True): raise ValueError('BELOW_MINIMUM_ORDER')
            target = target_price(average, rules)
            if not valid_size(target, rounded, rules): raise ValueError('BELOW_TARGET_MINIMUM')
            ratio = rounded/qty
            self._fill_tick(t, 'BUY', rounded, gross*ratio, now_ms, 'TAKER',
                            dict(book_received_ms=book['received_ms']), slip*ratio)
            # Gross performance is before both entry and exit additional slippage.
            t['gross_pnl'] += slip*ratio
            t.update(rules=rules, buy_average=average, take_profit_price=target,
                     stop_price=float(dec(average)*dec('.94')), last_bid=bids[0][0], mark_ms=now_ms)
            t['order_sequence'] += 1
            t['order'] = dict(id=f'{ident}:{t["order_sequence"]}', side='SELL', price=target,
                quantity=rounded, remaining=rounded, created_ms=now_ms, ready_ms=now_ms+250,
                active_ms=None, queue_ahead=None, reference_price=average,
                buy_average=average, sell_price_policy='FIXED_BUY_PLUS_12_PERCENT')
            self._event(t, now_ms, 'ORDER_PLACED', **t['order'])
            self._save(t)
            return True

    def _after_limit_fill(self, t, order, qty, price, ts, last, rules, liquidity, source):
        t['reason'] = 'TAKE_PROFIT_12'
        super()._after_limit_fill(t, order, qty, price, ts, last, rules, liquidity, source)
        if t['remaining_qty']==0:
            t.update(status='CLOSED', close_ms=ts, session_cash=0., last_error=None)
            self._event(t, ts, 'SESSION_ENDED', net_quote=t['realized_quote'])

    def _request_exit(self, t, ts, reason):
        self._cancel(t, ts, reason)
        if t['remaining_qty']>0:
            t.update(status='EXIT_PENDING', reason=reason, exit_ready_ms=ts)
            self._event(t, ts, 'MARKET_EXIT_REQUESTED', reason=reason)
        else:
            t.update(status='SKIPPED', reason=reason, session_cash=0., close_ms=ts)
        self._save(t)

    def check_stop(self, ident, book, now_ms):
        with self.lock, self.db:
            t = self._trade(ident)
            if not t or t['status']!='OPEN': return False
            bids, _ = checked_book(book, now_ms)
            t.update(last_bid=bids[0][0], mark_ms=book['received_ms'])
            if dec(bids[0][0])<=dec(t['stop_price']):
                # An observed executable bid triggers; a gap can fill below -6%.
                self._request_exit(t, book['requested_ms'], 'STOP_LOSS_6')
                return True
            self._save(t)
            return False

    def liquidate_all(self, now_ms):
        with self.lock, self.db:
            held = pending = 0
            for venue in VENUES:
                for t in self._active(venue):
                    if t['remaining_qty']>0: held += 1
                    else: pending += 1
                    if t['status']!='EXIT_PENDING': self._request_exit(t, now_ms, 'MANUAL_FAST_CLEAR')
            return dict(positions=held, canceled_entries=pending)

    def limit_step(self, ident, tape, book, rules, now_ms):
        with self.lock:
            t = self._trade(ident)
            # No entry/re-entry via the legacy limit-cycle path, even after close.
            if not t or t['status']!='OPEN' or not t.get('order'): return False
            return super().limit_step(ident, tape, book, rules, now_ms)
