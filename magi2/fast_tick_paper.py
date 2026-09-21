"""Durable 10-minute, one-tick limit cycles. Public evidence, never live orders."""
import hashlib
import json
import math

from magi2.fast_paper import (PaperLedger, ACTIVE, FEES_BPS, SLOT_KRW,
    MAX_POSITIONS, DEADLINE_MS, LATENCY_MS, ENTRY_TTL_MS, checked_book, market_sell)
from magi2.fast_tick_rules import adjacent, dec, floor_qty, valid_size

VERSION = 'fast-tick-cycle-v3'
MAKER_BPS = dict(upbit=5, bithumb=4, binance=10, kraken=40)
TAKER_BPS = dict(upbit=5, bithumb=4, binance=10, kraken=80)


def is_tick(t):
    return t.get('execution_version') in (VERSION,'fast-current-cycle-v4')


def validate_tape(tape, now_ms):
    if not (tape['requested_ms'] <= tape['received_ms'] <= now_ms
            and now_ms-tape['received_ms'] <= 3000
            and tape['received_ms']-tape['requested_ms'] <= 1500):
        raise ValueError('STALE_TRADE_RESPONSE')
    rows = tape['trades']
    if not rows: raise ValueError('NO_PUBLIC_TRADES')
    seen = set()
    for r in rows:
        if (not r['id'] or r['id'] in seen or not isinstance(r['buyer'],bool)
                or not all(math.isfinite(r[k]) and r[k]>0 for k in ('price','qty'))
                or r['ts'] > now_ms+1000):
            raise ValueError('INVALID_PUBLIC_TRADES')
        seen.add(r['id'])
    return sorted(rows, key=lambda r:(r['ts'],r['id']))


class TickLedger(PaperLedger):
    execution_version=VERSION

    def buy_price(self,last,rules):
        return adjacent(last,'BUY',rules)

    def __init__(self, path, now_ms):
        super().__init__(path, now_ms)
        with self.lock, self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS tick_order_events('
                'id INTEGER PRIMARY KEY, trade_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, '
                'event TEXT NOT NULL, payload TEXT NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS tick_order_session ON tick_order_events(trade_id,ts_ms)')

    def _event(self, t, ts, event, **payload):
        self.db.execute('INSERT INTO tick_order_events(trade_id,ts_ms,event,payload) VALUES(?,?,?,?)',
                        (t['id'],ts,event,self._json(payload)))

    def offer(self, ident, venue, symbol, signal_ms, now_ms, *, strategy_version='fast-volume-accel-v2'):
        with self.lock, self.db:
            if self._trade(ident): return False
            a = self._account(venue); active = self._active(venue)
            reserved = sum(t.get('session_cash',0) if is_tick(t) else
                           t['budget_quote'] if t['status']=='ENTRY_PENDING' else 0 for t in active)
            budget = SLOT_KRW/a['fx_krw_per_quote'] if a['funded_ms'] is not None else 0
            reason = ('STALE_SIGNAL' if not self.started_ms <= signal_ms <= now_ms <= signal_ms+ENTRY_TTL_MS else
                      'FX_NOT_READY' if a['funded_ms'] is None else
                      'SYMBOL_ALREADY_OPEN' if any(t['symbol']==symbol for t in active) else
                      'OVERDUE_EXIT' if any(t['remaining_qty']>0 and now_ms>=t['deadline_ms'] for t in active) else
                      'ALL_SLOTS_USED' if len(active)>=MAX_POSITIONS else
                      'INSUFFICIENT_CASH' if a['cash_quote']-reserved < budget-1e-8 else None)
            t = dict(id=ident, venue=venue, symbol=symbol, strategy_version=strategy_version,
                execution_version=self.execution_version, policy=self.execution_version, signal_ms=signal_ms,
                status='SKIPPED' if reason else 'ENTRY_PENDING',reason=reason,
                deadline_ms=signal_ms+DEADLINE_MS, budget_quote=budget, session_cash=budget if not reason else 0,
                ready_ms=now_ms+LATENCY_MS, fee_bps=TAKER_BPS[venue], maker_bps=MAKER_BPS[venue],
                slippage_bps=5, remaining_qty=0., remaining_cost=0., remaining_gross=0.,
                realized_quote=0., exit_proceeds=0., entry_cost=0., entry_qty=0., fees_paid=0.,
                gross_pnl=0., slippage_paid=0., forced_exit_pnl=0., cycles_completed=0, forced_exits=0,
                order_sequence=0, order=None, last_error=None,last_bid=None,mark_ms=None,
                last_book_hash=None, data_gaps=0, maker_fills=0, taker_fills=0)
            self._save(t)
            self._event(t,now_ms,'SESSION_SKIPPED' if reason else 'SESSION_STARTED',reason=reason)
            return reason is None

    def _cancel(self,t,ts,reason):
        if t.get('order'):
            self._event(t,ts,'ORDER_CANCELED',order_id=t['order']['id'],
                        remaining=t['order']['remaining'],reason=reason)
            t['order']=None

    def enter(self,ident,book,now_ms):
        # New sessions can only enter through the limit-order evidence path.
        with self.lock:
            t=self._trade(ident)
            if t and is_tick(t):return False
            return super().enter(ident,book,now_ms)

    def _expire(self,t,ts):
        self._cancel(t,ts,'SESSION_DEADLINE')
        if t['remaining_qty']>0:
            if t['status']!='EXIT_PENDING':
                t.update(status='EXIT_PENDING',reason='DEADLINE_10M',exit_ready_ms=ts)
                self._event(t,ts,'MARKET_EXIT_REQUESTED',trigger_delay_ms=ts-t['deadline_ms'])
        else:
            t.update(status='CLOSED' if t.get('entry_ms') else 'SKIPPED',
                     reason='SESSION_10M' if t.get('entry_ms') else 'LIMIT_UNFILLED_10M',
                     close_ms=ts,session_cash=0.)
            self._event(t,ts,'SESSION_ENDED',cycles=t['cycles_completed'],net_quote=t['realized_quote'])

    def advance(self,venue,now_ms):
        with self.lock,self.db:
            for t in self._active(venue):
                if is_tick(t):
                    if now_ms>=t['deadline_ms']: self._expire(t,now_ms)
                elif t['status']=='ENTRY_PENDING' and now_ms>t['signal_ms']+ENTRY_TTL_MS:
                    t.update(status='SKIPPED',reason='ENTRY_QUOTE_TIMEOUT')
                elif t['status']!='ENTRY_PENDING':
                    if now_ms>=t['deadline_ms']:
                        t.update(status='EXIT_PENDING',reason='DEADLINE_10M',
                                 exit_ready_ms=min(t.get('exit_ready_ms',now_ms),now_ms),deadline_missed=True)
                    elif now_ms>=t['exit_due_ms'] and t['status']=='OPEN':
                        t.update(status='EXIT_PENDING',reason='HOLD_5M',exit_ready_ms=now_ms+LATENCY_MS)
                self._save(t)

    def _fill_tick(self,t,side,qty,gross,ts,liquidity,source,slip=0.):
        fee = gross*(t['maker_bps'] if liquidity=='MAKER' else t['fee_bps'])/10000
        a = self._account(t['venue']); pnl = 0.
        if side=='BUY':
            cost=gross+fee
            if cost>min(a['cash_quote'],t['session_cash'])+1e-7: raise ValueError('CASH_INVARIANT')
            a['cash_quote']-=cost; t['session_cash']-=cost
            t['entry_cost']+=cost; t['entry_qty']+=qty
            t['remaining_qty']=float(dec(t['remaining_qty'])+dec(qty))
            t['remaining_cost']+=cost; t['remaining_gross']+=gross
            t.setdefault('entry_ms',ts); t['status']='OPEN'
        else:
            if qty>t['remaining_qty']+1e-10: raise ValueError('INVENTORY_INVARIANT')
            fraction=qty/t['remaining_qty']; basis=t['remaining_cost']*fraction
            basis_gross=t['remaining_gross']*fraction
            proceeds=gross-fee; pnl=proceeds-basis
            a['cash_quote']+=proceeds; t['session_cash']+=proceeds
            t['realized_quote']+=pnl; t['exit_proceeds']+=proceeds
            t['remaining_qty']=float(dec(t['remaining_qty'])-dec(qty))
            t['remaining_cost']-=basis; t['remaining_gross']-=basis_gross
            t['gross_pnl']+=gross+slip-basis_gross; t['last_sell_ms']=ts
            if liquidity=='MARKET_EXIT': t['forced_exit_pnl']+=pnl
            if t['remaining_qty']<=max(1e-14,qty*1e-10):
                t.update(remaining_qty=0.,remaining_cost=0.,remaining_gross=0.)
                if liquidity=='MARKET_EXIT':t['forced_exits']+=1
                else:t['cycles_completed']+=1
        t['fees_paid']+=fee; t['slippage_paid']+=slip
        t['maker_fills' if liquidity=='MAKER' else 'taker_fills']+=1
        self._save_account(a)
        fill=dict(mode='PAPER',quantity=qty,gross_quote=gross,fee_quote=fee,slippage_quote=slip,
                  average_price=gross/qty,liquidity=liquidity,policy=t['execution_version'],
                  reason=t['reason'],cycle=t['cycles_completed']+(1 if side=='BUY' else 0),
                  evidence=source)
        self.db.execute('INSERT INTO paper_fills(trade_id,venue,ts_ms,side,pnl_quote,fee_quote,payload) '
                        'VALUES(?,?,?,?,?,?,?)',(t['id'],t['venue'],ts,side,pnl,fee,self._json(fill)))
        self._event(t,ts,'FILL',side=side,quantity=qty,price=gross/qty,fee=fee,liquidity=liquidity)

    def _after_limit_fill(self,t,order,qty,price,ts,last,rules,liquidity,source):
        self._fill_tick(t,order['side'],qty,qty*price,ts,liquidity,source)
        order['remaining']=max(0.,float(dec(order['remaining'])-dec(qty)))
        if order['side']=='BUY':
            # Sell the acquired portion promptly once a valid sell order is possible.
            sell_price=adjacent(last,'SELL',rules)
            if valid_size(sell_price,t['remaining_qty'],rules):
                self._cancel(t,ts,'SELL_ACQUIRED_QUANTITY')
        elif t['remaining_qty']==0:
            self._event(t,ts,'ORDER_FILLED',order_id=order['id'])
            t['order']=None

    @staticmethod
    def _cursor(order,rows):
        order['page_ids']=[r['id'] for r in rows]
        order['cursor_ms']=rows[-1]['ts']
        order['cursor_ids']=[r['id'] for r in rows if r['ts']==rows[-1]['ts']]

    def limit_step(self,ident,tape,book,rules,now_ms):
        rows=validate_tape(tape,now_ms); last=rows[-1]['price']
        with self.lock,self.db:
            t=self._trade(ident)
            if not t or not is_tick(t) or t['status'] not in ACTIVE: return False
            if now_ms>=t['deadline_ms']:
                self._expire(t,now_ms);self._save(t);return False
            t['rules']=rules
            order=t['order']
            if order is None:
                side='SELL' if t['remaining_qty']>0 else 'BUY'
                price=adjacent(last,side,rules) if side=='SELL' else self.buy_price(last,rules)
                qty=floor_qty(t['remaining_qty'] if side=='SELL' else
                    min(t['budget_quote'],t['session_cash'])/(price*(1+t['fee_bps']/10000)),rules['step'])
                if not valid_size(price,qty,rules):
                    t['last_error']='BELOW_MINIMUM_ORDER'; self._save(t);return False
                t['order_sequence']+=1
                order=dict(id=f'{ident}:{t["order_sequence"]}',side=side,price=price,
                           quantity=qty,remaining=qty,created_ms=now_ms,ready_ms=now_ms+LATENCY_MS,
                           active_ms=None,queue_ahead=None,reference_price=last)
                t['order']=order;t['last_error']=None
                self._event(t,now_ms,'ORDER_PLACED',**order)
                self._save(t);return True
            if now_ms<order['ready_ms']: return False
            if order['active_ms'] is None or order.get('resync'):
                if book is None: return False
                bids,asks=checked_book(book,now_ms,order['ready_ms'])
                t.update(last_bid=bids[0][0],mark_ms=book['received_ms'])
                own=bids if order['side']=='BUY' else asks
                opposite=asks if order['side']=='BUY' else bids
                visible=(order['price']>=bids[-1][0] if order['side']=='BUY' else order['price']<=asks[-1][0])
                if not visible:
                    t['last_error']='QUEUE_OUTSIDE_VISIBLE_DEPTH';self._save(t);return False
                queue=sum(q for p,q in own if math.isclose(p,order['price'],rel_tol=1e-12))
                if order.get('resync'):
                    order['queue_ahead']=max(order['queue_ahead'] or 0,queue)
                    order['resync']=False;self._cursor(order,rows)
                    t['last_error']=None;self._save(t);return False
                order.update(active_ms=now_ms,queue_ahead=queue)
                self._cursor(order,rows)
                self._event(t,now_ms,'ORDER_ACTIVE',order_id=order['id'],queue_ahead=queue,
                            activation_delay_ms=now_ms-order['created_ms'])
                # A limit can become marketable during submission latency.
                # It executes only at displayed prices within the stated limit.
                for p,size in opposite:
                    eligible=p<=order['price'] if order['side']=='BUY' else p>=order['price']
                    if not eligible:break
                    qty=floor_qty(min(order['remaining'],size*.1),rules['step'])
                    if qty<=0:continue
                    self._after_limit_fill(t,order,qty,p,now_ms,last,rules,'TAKER',
                                           dict(book_received_ms=book['received_ms']))
                    if t['order'] is None:break
                self._save(t);return True
            if not set(order['page_ids']).intersection(r['id'] for r in rows):
                order['resync']=True;t['data_gaps']+=1;t['last_error']='TRADE_PAGE_GAP'
                self._event(t,now_ms,'DATA_GAP',order_id=order['id'])
                self._save(t);return False
            new=[r for r in rows if r['ts']>order['cursor_ms'] or
                 (r['ts']==order['cursor_ms'] and r['id'] not in order['cursor_ids'])]
            for r in new:
                if r['ts']<order['active_ms'] or r['ts']>=t['deadline_ms']:continue
                eligible=(not r['buyer'] and r['price']<=order['price']) if order['side']=='BUY' else (
                           r['buyer'] and r['price']>=order['price'])
                if not eligible:continue
                used=min(order['queue_ahead'],r['qty']);order['queue_ahead']-=used
                qty=floor_qty(min(order['remaining'],r['qty']-used),rules['step'])
                if qty<=0:continue
                self._after_limit_fill(t,order,qty,order['price'],now_ms,last,rules,'MAKER',
                                      dict(trade_id=r['id'],exchange_ms=r['ts'],received_ms=tape['received_ms']))
                if t['order'] is None:break
            if t['order'] is not None:self._cursor(order,rows)
            t['last_error']=None;self._save(t);return True

    def exit(self,ident,book,now_ms):
        with self.lock,self.db:
            t=self._trade(ident)
            if not t or not is_tick(t): return super().exit(ident,book,now_ms)
            if t['status']!='EXIT_PENDING':return False
            bids,_=checked_book(book,now_ms,t['exit_ready_ms'])
            rules=t.get('rules')
            if not rules:raise ValueError('MISSING_EXIT_RULES')
            qty=floor_qty(t['remaining_qty'],rules.get('market_step',rules['step']))
            if not valid_size(bids[0][0],qty,rules,market=True):
                t['last_error']='DUST_BELOW_MARKET_MINIMUM';self._save(t);return False
            fingerprint=hashlib.sha256(self._json(bids).encode()).hexdigest()
            if fingerprint==t['last_book_hash']:return False
            depth=[(p,floor_qty(size,rules.get('market_step',rules['step']))) for p,size in bids]
            sold,gross,_,slip=market_sell(depth,qty,t['fee_bps']/10000,t['slippage_bps']/10000)
            if sold<=0:
                t['last_error']='NO_EXECUTABLE_MARKET_DEPTH';self._save(t);return False
            self._fill_tick(t,'SELL',sold,gross,book['received_ms'],'MARKET_EXIT',
                            dict(book_received_ms=book['received_ms']),slip)
            t.update(last_book_hash=fingerprint,last_bid=bids[0][0],mark_ms=book['received_ms'])
            if t['remaining_qty']==0:
                t.update(status='CLOSED',close_ms=book['received_ms'],session_cash=0.,last_error=None,
                         deadline_delay_ms=max(0,book['received_ms']-t['deadline_ms']))
                self._event(t,now_ms,'SESSION_ENDED',cycles=t['cycles_completed'],net_quote=t['realized_quote'])
            else:t['last_error']='PARTIAL_EXIT_DEPTH'
            self._save(t);return True

    def recent_sessions(self,now_ms,limit=10):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM paper_trades WHERE signal_ms<=? AND "
                "(entry_ms IS NOT NULL OR status='ENTRY_PENDING' OR "
                "json_extract(payload,'$.reason')='LIMIT_UNFILLED_10M') "
                "ORDER BY signal_ms DESC,id DESC LIMIT ?",(now_ms,limit))]


class CurrentTickLedger(TickLedger):
    """v4 buys at the observed current trade price, without chasing the ask."""
    execution_version='fast-current-cycle-v4'

    def buy_price(self,last,rules):
        from magi2.fast_tick_rules import tick_at
        p=dec(last)
        if p % tick_at(last,rules):raise ValueError('CURRENT_PRICE_OFF_TICK')
        if p<dec(rules.get('min_price',0)) or (rules.get('max_price') and p>dec(rules['max_price'])):
            raise ValueError('PRICE_OUTSIDE_LIMITS')
        return float(p)
