"""Capture-price-capped FAST paper entries; no exchange orders are submitted."""
from decimal import ROUND_FLOOR

from magi2.fast_paper import ENTRY_TTL_MS, checked_book
from magi2.fast_target_paper import TargetLedger, target_price
from magi2.fast_tick_rules import adjacent, dec, tick_at, valid_size

VERSION = 'fast-capture-limit-v6'
POLICY = 'CAPTURE_LAST_PLUS_ONE_TICK'


def capped_buy(asks, budget, fee_bps, limit, rules):
    """Use only displayed depth at/below the limit, with exact lot rounding.

    Partial depth is allowed. Unspent cash is released after this single fill.
    Limit buys have no synthetic price uplift; fees remain separate costs.
    """
    remaining = dec(budget) / (1 + dec(fee_bps) / 10000)
    step = dec(rules['step'])
    if step <= 0:
        raise ValueError('INVALID_QUANTITY_STEP')
    maximum = dec(rules['max_qty']) if rules.get('max_qty') else None
    quantity = gross = dec(0)
    levels = []
    for raw_price, raw_size in asks:
        price, size = dec(raw_price), dec(raw_size)
        if price > dec(limit):
            break
        available = min(size, remaining / price)
        if maximum is not None:
            available = min(available, maximum - quantity)
        qty = (available / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if qty <= 0:
            continue
        paid = qty * price
        quantity += qty
        gross += paid
        remaining -= paid
        levels.append(dict(price=float(price), quantity=float(qty)))
    return float(quantity), float(gross), levels


class CaptureLimitLedger(TargetLedger):
    execution_version = VERSION
    entry_price_policy = POLICY

    def advance(self, venue, now_ms):
        expired = super().advance(venue, now_ms)
        with self.lock, self.db:
            for ident in expired:
                t = self._trade(ident)
                if t.get('entry_price_policy') == POLICY:
                    t['reason'] = 'ENTRY_LIMIT_UNFILLED'
                    self._event(t, now_ms, 'ENTRY_EXPIRED', reason=t['reason'],
                                entry_limit_price=t.get('entry_limit_price'))
                    self._save(t)
        return expired

    def enter_market(self, ident, book, rules, now_ms):
        # Retain the worker interface, but never route a ranked entry to market_buy.
        with self.lock, self.db:
            if not self.control()['enabled']:
                return False
            t = self._trade(ident)
            if not t or t['status'] != 'ENTRY_PENDING':
                return False
            if now_ms > t['signal_ms'] + ENTRY_TTL_MS:
                self.advance(t['venue'], now_ms)
                return False
            bids, asks = checked_book(book, now_ms, t['ready_ms'])
            if (t.get('entry_price_policy') != POLICY or not t.get('capture_price')
                    or t.get('capture_ms') is None
                    or not 0 <= t['signal_ms'] - t['capture_ms'] <= ENTRY_TTL_MS):
                t.update(status='SKIPPED', reason='MISSING_CAPTURE_PRICE',
                         session_cash=0., close_ms=now_ms)
                self._save(t)
                return False
            reference = dec(t['capture_price'])
            if 'entry_limit_price' not in t:
                size = tick_at(reference, rules)
                t.update(entry_limit_price=adjacent(reference, 'SELL', rules),
                         entry_tick_price=float(reference), entry_tick_size=float(size),
                         entry_tick_pct=float(size / reference * 100),
                         entry_tick_limit_pct=self.entry_tick_limit_pct,
                         entry_tick_rule_source=rules.get('source'))
                self._event(t, now_ms, 'ENTRY_LIMIT_SET', capture_price=float(reference),
                            capture_ms=t['capture_ms'], limit_price=t['entry_limit_price'],
                            expires_ms=t['signal_ms'] + ENTRY_TTL_MS)
            t.update(entry_book_ms=book['received_ms'], entry_bid=bids[0][0],
                     entry_ask=asks[0][0], last_bid=bids[0][0], mark_ms=now_ms)
            if dec(t['entry_tick_size']) * 100 >= reference * dec(self.entry_tick_limit_pct):
                t.update(status='SKIPPED', reason='ENTRY_TICK_TOO_LARGE', session_cash=0.,
                         close_ms=now_ms, last_error=None)
                self._event(t, now_ms, 'ENTRY_BLOCKED', reason=t['reason'],
                            **{k: v for k, v in t.items() if k.startswith('entry_tick_')})
                self._save(t)
                return False
            limit = dec(t['entry_limit_price'])
            # If exchange rules change during the wait, reject rather than raise the cap.
            if limit % tick_at(limit, rules):
                raise ValueError('ENTRY_LIMIT_OFF_CURRENT_GRID')
            qty, gross, levels = capped_buy(asks, t['budget_quote'], t['fee_bps'], limit, rules)
            if qty <= 0 or not valid_size(gross / qty, qty, rules):
                reason = 'ASK_ABOVE_CAPTURE_LIMIT' if dec(asks[0][0]) > limit else 'INSUFFICIENT_LIMIT_DEPTH'
                if t.get('entry_wait_reason') != reason:
                    self._event(t, now_ms, 'ENTRY_WAITING', reason=reason,
                                limit_price=float(limit), best_ask=asks[0][0])
                t.update(entry_wait_reason=reason, last_error=None)
                self._save(t)
                return False
            average = gross / qty
            target = target_price(average, rules)
            if not valid_size(target, qty, rules):
                raise ValueError('BELOW_TARGET_MINIMUM')
            self._fill_tick(t, 'BUY', qty, gross, now_ms, 'TAKER',
                            dict(book_received_ms=book['received_ms'],
                                 capture_price=float(reference), capture_ms=t['capture_ms'],
                                 limit_price=float(limit), entry_price_policy=POLICY, levels=levels))
            released = t['session_cash']
            t.update(rules=rules, buy_average=average, take_profit_price=target,
                     stop_price=float(dec(average) * dec('.94')), session_cash=0.,
                     entry_unspent_quote=released, entry_fill_levels=levels,
                     entry_wait_reason=None, last_error=None)
            t['order_sequence'] += 1
            t['order'] = dict(id=f'{ident}:{t["order_sequence"]}', side='SELL', price=target,
                             quantity=qty, remaining=qty, created_ms=now_ms, ready_ms=now_ms+250,
                             active_ms=None, queue_ahead=None, reference_price=average,
                             buy_average=average, sell_price_policy='FIXED_BUY_PLUS_12_PERCENT')
            self._event(t, now_ms, 'ENTRY_REMAINDER_RELEASED', unspent_quote=released)
            self._event(t, now_ms, 'ORDER_PLACED', **t['order'])
            self._save(t)
            return True

