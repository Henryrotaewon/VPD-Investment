"""User-run FAST live-order building blocks; NOT started by deployed runners.

Budget is shared across venues, includes outstanding reservations and fees.
Uncertain POST outcomes remain reserved and must be reconciled by identifier.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import sqlite3
import threading
import time
import uuid
from .fast_audit import FastAudit,order_result


def decimal(value):
    try:x=Decimal(str(value))
    except (InvalidOperation,ValueError):raise ValueError('INVALID_MONEY')
    if not x.is_finite() or x<0:raise ValueError('INVALID_MONEY')
    return x

class FastBudget:
    """Durable shared reservation book. Pending/unknown outcomes never free capital."""
    def __init__(self,path):
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row;self.lock=threading.RLock()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS fast_orders(id TEXT PRIMARY KEY,signal TEXT UNIQUE,payload TEXT,state TEXT,reserved TEXT,result TEXT)')
        self.db.commit()
        self.audit=FastAudit(connection=self.db,lock=self.lock)
    def reserve(self,signal,payload,snapshot,amount,fee_rate,now=None):
        now=time.time_ns()//1000000 if now is None else now
        if not snapshot.get('complete') or not 0<=now-int(snapshot['observed_ts_ms'])<=5000:
            raise ValueError('INCOMPLETE_OR_STALE_CAPITAL')
        equity=decimal(snapshot['total_equity_krw']);cash=decimal(snapshot['available_cash_krw'])
        held=decimal(snapshot['fast_position_value_krw']);external=decimal(snapshot['external_fast_pending_krw'])
        amount=decimal(amount);fee=decimal(fee_rate)
        if amount<=0 or fee>Decimal('.01'):raise ValueError('INVALID_ORDER_SIZE_OR_FEE')
        cost=amount*(1+fee)
        with self.lock:
            try:
                self.db.execute('BEGIN IMMEDIATE')
                previous=self.db.execute('SELECT id FROM fast_orders WHERE signal=?',(signal,)).fetchone()
                if previous:
                    self.db.rollback();return previous['id'],False
                rows=self.db.execute("SELECT reserved FROM fast_orders WHERE state!='RECONCILED'").fetchall()
                reserved=sum((decimal(r['reserved']) for r in rows),Decimal(0))
                # Strictly below 5%, even after adding the estimated buy fee.
                if held+external+reserved+cost>=equity*Decimal('.05'):raise ValueError('FAST_TOTAL_5_PERCENT_LIMIT')
                # Conservative: pending capital is subtracted even if a broker already locked it.
                if cost+reserved>cash:raise ValueError('INSUFFICIENT_AVAILABLE_CASH')
                ident='fast-'+uuid.uuid4().hex
                self.db.execute('INSERT INTO fast_orders VALUES(?,?,?,?,?,?)',(ident,signal,json.dumps(payload),'RESERVED',str(cost),None))
                self.db.commit();return ident,True
            except BaseException:
                self.db.rollback();raise
    def state(self,ident,state,result=None):
        if state not in ('SENDING','UNKNOWN','ACKNOWLEDGED','TERMINAL','RECONCILED'):raise ValueError('INVALID_STATE')
        with self.lock,self.db:
            self.db.execute('UPDATE fast_orders SET state=?,result=? WHERE id=?',(state,json.dumps(result),ident))
    def get(self,ident):
        with self.lock:
            row=self.db.execute('SELECT * FROM fast_orders WHERE id=?',(ident,)).fetchone()
            return dict(row) if row else None
    def close(self):self.db.close()

class UpbitFastOrders:
    """Explicit opt-in client. Tests must supply a mock HTTP adapter.

A one-second strategy loop should call reconcile on outstanding identifiers,
never resubmit a timed-out POST. IOC limits bound the requested price;
actual partial fills and fees must be applied before releasing reservations.
"""
    def __init__(self,adapter,budget,enabled=False):self.adapter=adapter;self.budget=budget;self.enabled=enabled
    def buy(self,signal,market,ask,amount,snapshot,fee_rate,min_order):
        if not self.enabled:
            self.budget.audit.record('ORDER_SKIPPED',signal,'upbit',market,'DISABLED',reason='LIVE_DISABLED')
            return {'status':'BLOCKED','reason':'LIVE_DISABLED'}
        price=decimal(ask);amount=decimal(amount)
        if not market.startswith('KRW-') or price<=0 or amount<decimal(min_order):raise ValueError('INVALID_KRW_ORDER')
        quantity=(amount/price).quantize(Decimal('.00000001'),rounding=ROUND_DOWN)
        if quantity*price<decimal(min_order):raise ValueError('BELOW_MINIMUM_AFTER_ROUNDING')
        body={'market':market,'side':'bid','ord_type':'limit','price':str(price),
              'volume':str(quantity),'time_in_force':'ioc','smp_type':'cancel_taker'}
        try:ident,new=self.budget.reserve(signal,body,snapshot,amount,fee_rate)
        except ValueError as exc:
            self.budget.audit.record('ORDER_SKIPPED',signal,'upbit',market,'LIVE',reason=str(exc),
                                     capital={k:snapshot.get(k) for k in ('complete','observed_ts_ms','total_equity_krw','available_cash_krw','fast_position_value_krw','external_fast_pending_krw')})
            raise
        if not new:return {'status':'EXISTING','identifier':ident}
        body['identifier']=ident
        self.budget.audit.record('ORDER_REQUEST',signal,'upbit',market,'LIVE',order_id=ident,request=body,
                                 capital={k:snapshot.get(k) for k in ('observed_ts_ms','total_equity_krw','available_cash_krw','fast_position_value_krw','external_fast_pending_krw')})
        self.budget.state(ident,'SENDING')
        started=time.monotonic()
        try:
            headers={**self.adapter._auth(body),'Content-Type':'application/json'}
            r=self.adapter.http.post('https://api.upbit.com/v1/orders',json=body,headers=headers,timeout=(3,5))
            r.raise_for_status();data=r.json()
            self.budget.state(ident,'ACKNOWLEDGED',data)
            self.budget.audit.record('ORDER_RESPONSE',signal,'upbit',market,'LIVE',order_id=ident,
                                     duration_ms=round((time.monotonic()-started)*1000),result=order_result(data))
            return {'status':'ACKNOWLEDGED','identifier':ident,'order':data}
        except Exception as exc:
            self.budget.state(ident,'UNKNOWN')
            self.budget.audit.record('ORDER_OUTCOME_UNKNOWN',signal,'upbit',market,'LIVE',order_id=ident,
                                     duration_ms=round((time.monotonic()-started)*1000),reason='RECONCILE_DO_NOT_RESUBMIT',
                                     result={'error_type':type(exc).__name__,'http_status':getattr(getattr(exc,'response',None),'status_code',None)})
            return {'status':'UNKNOWN','identifier':ident,'action':'RECONCILE_DO_NOT_RESUBMIT'}
    def reconcile(self,ident):
        if not self.enabled:return {'status':'BLOCKED','reason':'LIVE_DISABLED'}
        row=self.budget.get(ident)
        if not row:raise ValueError('UNKNOWN_LOCAL_IDENTIFIER')
        params={'identifier':ident}
        try:
            r=self.adapter.http.get('https://api.upbit.com/v1/order',params=params,headers=self.adapter._auth(params),timeout=(3,5))
            r.raise_for_status();data=r.json()
        except Exception as exc:
            self.budget.audit.record('ORDER_RECONCILE_FAILED',row['signal'],'upbit',json.loads(row['payload'])['market'],'LIVE',order_id=ident,
                                     result={'error_type':type(exc).__name__,'http_status':getattr(getattr(exc,'response',None),'status_code',None)},reason='RESERVATION_RETAINED')
            raise
        self.budget.state(ident,'TERMINAL' if data.get('state') in ('done','cancel') else 'ACKNOWLEDGED',data)
        self.budget.audit.record('ORDER_RECONCILED',row['signal'],'upbit',json.loads(row['payload'])['market'],'LIVE',
                                 order_id=ident,result=order_result(data))
        return data
