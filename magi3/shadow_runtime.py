"""Long-only shadow IOC execution. No private order API is reachable here."""
import json
import math
import re
import time
import uuid
from .runtime_store import encode


def stamp():return time.time_ns()//1000000


def sweep(levels,amount,by_quantity=False):
    qty=notional=0.
    left=amount
    for price,size in levels:
        take=min(size,left if by_quantity else left/price)
        qty+=take;notional+=take*price
        left-=take if by_quantity else take*price
        if left<=1e-10:break
    return qty,notional,left>max(1e-10,amount*1e-10)


def validate(signal,now):
    if signal.get('source')!='MAGI2' or signal.get('mode')!='SHADOW' or signal.get('side')!='BUY':
        raise ValueError('SHADOW_BUY_INTENT_REQUIRED')
    if not re.fullmatch(r'[A-Za-z0-9:_-]{1,160}',signal.get('id','')):raise ValueError('INVALID_ID')
    if signal.get('venue') not in ('upbit','bithumb','binance','kraken'):raise ValueError('INVALID_VENUE')
    if not re.fullmatch(r'[A-Z0-9]{1,20}',signal.get('asset','')):raise ValueError('INVALID_ASSET')
    created=int(signal['created_ts_ms']);expires=int(signal['expires_ts_ms'])
    if created>now+5000 or now>=expires or expires<=created or expires-created>120000:raise ValueError('EXPIRED_OR_INVALID_TIME')
    for key in ('notional_krw','max_holding_seconds'):
        if isinstance(signal[key],bool) or not math.isfinite(float(signal[key])) or float(signal[key])<=0:
            raise ValueError('INVALID_SIZE_OR_HOLD')
    if float(signal['max_holding_seconds'])>86400:raise ValueError('HOLD_TOO_LONG')
    tags=signal.get('strategy_tags')
    if not isinstance(tags,list) or not tags or any(x not in ('VPD','FAST','WAVE') for x in tags):
        raise ValueError('INVALID_STRATEGY_TAGS')


class ShadowEngine:
    def __init__(self,store,market,config,fee_bps=10):
        if config.mode not in ('SHADOW','DRY_RUN') or config.live_enabled:raise ValueError('LIVE_RUNTIME_DISABLED')
        if not math.isfinite(fee_bps) or not 0<=fee_bps<=100:raise ValueError('INVALID_FEE_MODEL')
        if min(config.max_order_krw,config.max_total_exposure_krw,config.max_daily_loss_krw)<=0:raise ValueError('INVALID_LIMITS')
        self.store=store;self.market=market;self.config=config;self.fee=fee_bps/10000

    def ingest(self,s,now=None):
        now=stamp() if now is None else now
        # Encode before recording: NaN and unserialisable payloads never enter the ledger.
        payload=encode(s)
        try:validate(s,now)
        except (ValueError,KeyError,TypeError,OverflowError):return 'INVALID_OR_EXPIRED'
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?)',(s['id'],payload,'PENDING',None,now))
            row=db.execute('SELECT * FROM signals WHERE id=?',(s['id'],)).fetchone()
            if row['status']!='PENDING':return row['status']
            payload=row['payload']
            s=json.loads(payload) # a repeated ID cannot change the original intent
        try:validate(s,now)
        except (ValueError,KeyError,TypeError,OverflowError):
            return self.reject(s['id'],'EXPIRED')
        if self.config.mode!='SHADOW':return self.reject(s['id'],'DRY_RUN')
        try:
            book=self.market.book(s['venue'],s['asset'],fresh=True)
            if not 0<=stamp()-book.received_ms<=10000:raise ValueError('STALE_BOOK')
        except Exception:return 'PENDING_BOOK'
        # Revalidate after network wait, not just before it.
        now=max(now,stamp())
        if now>=s['expires_ts_ms']:return self.reject(s['id'],'EXPIRED')
        budget=float(s['notional_krw'])
        qty,notional,partial=sweep(book.asks,budget)
        fee=notional*self.fee;cost=notional+fee
        with self.store.transaction() as db:
            if db.execute('SELECT status FROM signals WHERE id=?',(s['id'],)).fetchone()[0]!='PENDING':return 'DUPLICATE'
            cash=float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
            exposure=db.execute('SELECT COALESCE(SUM(cost),0) FROM positions WHERE qty>0').fetchone()[0]
            day=(now+9*3600000)//86400000*86400000-9*3600000
            realized=db.execute('SELECT COALESCE(SUM(realized),0) FROM fills WHERE ts_ms>=?',(day,)).fetchone()[0]
            reason=('KILL_SWITCH' if self.config.kill_switch else
                    'MAX_ORDER' if budget>self.config.max_order_krw else
                    'EXPOSURE_LIMIT' if exposure+cost>self.config.max_total_exposure_krw else
                    'DAILY_REALIZED_LOSS' if realized<=-self.config.max_daily_loss_krw else
                    'INSUFFICIENT_VIRTUAL_CASH' if cost>cash else
                    'NO_LIQUIDITY' if qty<=0 else None)
            if reason:
                db.execute('UPDATE signals SET status=?,reason=? WHERE id=?',('REJECTED',reason,s['id']))
                return reason
            ident=uuid.uuid4().hex
            db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',(ident,s['id'],ident,'BUY',s['venue'],s['asset'],'PARTIAL_IOC' if partial else 'FILLED','SHADOW',now))
            db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,ident,qty,notional,fee,0.,now))
            db.execute('INSERT INTO positions VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,s['id'],s['venue'],s['asset'],qty,cost,now,now+int(float(s['max_holding_seconds'])*1000),encode(s['strategy_tags']),payload))
            db.execute('UPDATE meta SET value=? WHERE key=?',(encode(cash-cost),'cash'))
            db.execute('UPDATE signals SET status=? WHERE id=?',('FILLED',s['id']))
        return 'PARTIAL_IOC' if partial else 'FILLED'

    def reject(self,ident,reason):
        with self.store.transaction() as db:
            db.execute("UPDATE signals SET status='REJECTED',reason=? WHERE id=? AND status='PENDING'",(reason,ident))
        return reason

    def recover_pending(self):
        for row in self.store.rows("SELECT payload FROM signals WHERE status='PENDING' ORDER BY created_ms LIMIT 100"):
            signal=json.loads(row['payload'])
            if stamp()>=signal['expires_ts_ms']:self.reject(signal['id'],'EXPIRED')
            else:self.ingest(signal)

    def exit_due(self,now=None):
        now=stamp() if now is None else now
        outcomes=[]
        for p in self.store.positions():
            if p['deadline_ms']>now:continue
            try:
                book=self.market.book(p['venue'],p['asset'],fresh=True)
                if not 0<=stamp()-book.received_ms<=10000:raise ValueError('STALE_BOOK')
            except Exception:
                outcomes.append('EXIT_PENDING_BOOK');continue
            with self.store.transaction() as db:
                current=db.execute('SELECT * FROM positions WHERE id=?',(p['id'],)).fetchone()
                if current['qty']<=0:continue
                qty,notional,partial=sweep(book.bids,current['qty'],True)
                if qty<=0:continue
                fee=notional*self.fee;allocated=current['cost']*qty/current['qty']
                realized=notional-fee-allocated;ident=uuid.uuid4().hex
                db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',(ident,p['signal_id'],p['id'],'SELL',p['venue'],p['asset'],'PARTIAL_IOC' if partial else 'FILLED','MAX_HOLDING_TIME',now))
                db.execute('INSERT INTO fills VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,ident,qty,notional,fee,realized,now))
                db.execute('UPDATE positions SET qty=?,cost=? WHERE id=?',(max(0,current['qty']-qty) if partial else 0,max(0,current['cost']-allocated) if partial else 0,p['id']))
                cash=float(db.execute("SELECT value FROM meta WHERE key='cash'").fetchone()[0])
                db.execute('UPDATE meta SET value=? WHERE key=?',(encode(cash+notional-fee),'cash'))
                outcomes.append('PARTIAL_IOC' if partial else 'CLOSED')
        return outcomes

    def report(self):
        positions=self.store.positions();known=0.;complete=True
        for p in positions:
            p['strategy_tags']=json.loads(p.pop('tags'));p.pop('evidence')
            p.update(value_krw=None,unrealized_pnl_krw=None)
            try:
                quote=self.market.mark(p['venue'],p['asset'])
                price=float(quote['price_krw'])
                if not math.isfinite(price) or price<=0 or not 0<=stamp()-quote['received_ts_ms']<=60000:raise ValueError('STALE')
                value=p['qty']*price;known+=value
                p.update(value_krw=value,unrealized_pnl_krw=value-p['cost'],price_ts_ms=quote['received_ts_ms'])
            except Exception:complete=False
        totals=self.store.rows('SELECT COALESCE(SUM(realized),0) AS realized,COALESCE(SUM(fee),0) AS fees FROM fills')[0]
        strategies={}
        for p in self.store.rows('SELECT p.id,p.tags,COALESCE(SUM(f.realized),0) AS realized FROM positions p LEFT JOIN orders o ON o.position_id=p.id LEFT JOIN fills f ON f.order_id=o.id GROUP BY p.id'):
            tags=list(dict.fromkeys(json.loads(p['tags'])))
            realized=p['realized']
            opened=next((x for x in positions if x['id']==p['id']),None)
            for tag in tags:
                row=strategies.setdefault(tag,{'realized_pnl_krw':0.,'unrealized_pnl_krw':0.})
                row['realized_pnl_krw']+=realized/len(tags)
                value=opened['unrealized_pnl_krw'] if opened else 0.
                if value is None:row['unrealized_pnl_krw']=None
                elif row['unrealized_pnl_krw'] is not None:row['unrealized_pnl_krw']+=value/len(tags)
        cash=self.store.get('cash')
        return {'schema':'magi3-shadow-v1','mode':'SHADOW','generated_ts_ms':stamp(),
                'cash_krw':cash,'initial_cash_krw':self.store.get('initial_cash'),
                'equity_krw':cash+known if complete else None,'known_subtotal_krw':cash+known,
                'realized_pnl_krw':totals['realized'],'fees_krw':totals['fees'],
                'strategy_summary':strategies,'attribution':'Equal split across distinct tags; not independent strategy performance',
                'fee_bps_per_side':self.fee*10000,'positions':positions,'complete':complete,
                'unrealized_pnl_krw':sum(p['unrealized_pnl_krw'] for p in positions) if complete else None,
                'note':'Virtual IOC fills from public depth. Unfilled quantity cancelled. No latency/queue impact model. Equity uses midpoint; unrealized PnL excludes prospective exit fees. Risk exposure is cost basis; daily loss is realized KST PnL.'}
