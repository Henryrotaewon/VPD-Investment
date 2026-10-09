import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from magi2.fast_models.bear import BearPaper, features, FIVE
from magi2.fast_models.derivatives import Derivatives, fill, quantize
from magi2.fast_models.market import quote, fresh, Collector
from magi2.fast_models.metrics import Metrics, DAY, INITIAL
from magi2.fast_models.report import view, load
from magi1.fast_observe import Observer

START=20000*DAY+100000


def q(venue='binance',kind='perp',price=100,ts=START,asset='BTC'):
    currency='KRW' if venue in ('upbit','bithumb') else 'USD' if venue=='kraken' else 'USDT'
    return quote(venue,kind,asset,currency,asset,[(price-.01,100)],[(price+.01,100)],ts,20,ts,
                 step=.001,min_qty=.001,min_notional=1,meta_ts=ts)


def fx():return {k:dict(bid=1.,ask=1.,ts=START) for k in ('KRW','USD','USDT')}


def feed(ts=START,venue='binance',rate=.001):
    return dict(ts=ts,mark=100.,index=100.,rate=rate,next=START+1000000,events=[],rates={str(START//3600000*3600000):rate})


class MetricsTests(unittest.TestCase):
    def test_intraday_and_lifetime_drawdown_not_from_daily_closes(self):
        db=sqlite3.connect(':memory:');m=Metrics(db,START)
        for i,n in enumerate([3300000,2800000,3200000],1):m.mark(START+i*10000,n,True)
        self.assertAlmostEqual(m.s['mdd'],(1-2800000/3300000)*100)
        d=json.loads(db.execute('SELECT payload FROM model_days').fetchone()[0])
        self.assertAlmostEqual(d['mdd'],m.s['mdd'])
    def test_gap_does_not_invent_daily_return_or_reduce_mdd(self):
        db=sqlite3.connect(':memory:');m=Metrics(db,START)
        m.mark(START+2*DAY,2700000,True)
        d=json.loads(db.execute('SELECT payload FROM model_days ORDER BY day DESC').fetchone()[0])
        self.assertIsNone(d['base']);self.assertTrue(d['gap']);self.assertTrue(m.s['gap'])
        self.assertAlmostEqual(m.s['mdd'],10.)
    def test_stale_mark_never_changes_high_water(self):
        db=sqlite3.connect(':memory:');m=Metrics(db,START)
        m.mark(START+10000,9000000,False)
        self.assertEqual(m.s['peak'],INITIAL)
    def test_idempotent_closed_pair_counts_once(self):
        db=sqlite3.connect(':memory:');m=Metrics(db,START)
        for _ in range(2):m.trade('pair',START,'BASIS',100,{})
        self.assertEqual(db.execute('SELECT count(*) FROM model_trades').fetchone()[0],1)


class DerivativesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=Derivatives(Path(self.tmp.name)/'derivatives.sqlite3',START)
        self.p.initialize_wallets(fx(),START)
    def tearDown(self):self.p.db.close();self.tmp.cleanup()
    def test_wallets_initial_total_and_no_borrow(self):
        self.assertEqual(sum(w['cash'] for w in self.p.s['wallets'].values()),INITIAL)
        a=q(price=100);a['bids']=[(100,20000)];self.p.reserve('x','SHORT',None,a,10000,fx(),START,{})
        self.assertFalse(self.p.s['pending'])
    def test_shared_capital_asset_exclusivity(self):
        a=q();s=q('upbit','spot',80)
        self.p.reserve('pair','BASIS',s,a,100,fx(),START,dict(mean=0))
        self.p.reserve('short','SHORT',None,a,100,fx(),START,{})
        self.assertEqual(list(self.p.s['pending']),['pair'])
    def test_causal_fill_and_stale_pair_does_not_spend(self):
        a=q();s=q('upbit','spot',80);books={a['id']:a,s['id']:s};f={a['id']:feed()}
        self.p.reserve('pair','BASIS',s,a,100,fx(),START,dict(mean=0))
        wallets=copy.deepcopy(self.p.s['wallets'])
        self.p.enter_pending(books,fx(),f,START+1000)
        self.assertFalse(self.p.s['positions']);self.assertEqual(wallets,self.p.s['wallets'])
        for x in books.values():x.update(ts=START+2000,exchange_ts=START+2000)
        self.p.enter_pending(books,fx(),f,START+2000)
        self.assertEqual(len(self.p.s['positions']),1)
        self.assertLess(sum(w['cash'] for w in self.p.s['wallets'].values()),INITIAL)
    def test_short_requires_current_bear_regime_on_fill(self):
        a=q();self.p.reserve('s','SHORT',None,a,10,fx(),START,{})
        a.update(ts=START+1000,exchange_ts=START+1000)
        self.p.enter_pending({a['id']:a},fx(),{a['id']:feed()},START+1000,dict(state='OTHER',ts=START))
        self.assertFalse(self.p.s['positions']);self.assertFalse(self.p.s['pending'])
    def position(self,venue='binance'):
        a=q(venue);return dict(id='s',entry_ms=START,qty=2.,funding_cursor=START,next_funding=START+10000,
              legs=[dict(instrument=a['id'],quote=a['quote'],funding=0.)])
    def test_binance_funding_positive_negative_and_idempotence(self):
        p=self.position();d=feed(START+10000);d['events']=[dict(ts=START+10000,mark=100,rate=-.01)]
        self.assertTrue(self.p.funding(p,{'binance:perp:BTC':d},START+10000))
        self.assertEqual(p['legs'][0]['funding'],-2.)
        self.p.funding(p,{'binance:perp:BTC':d},START+10000)
        self.assertEqual(p['legs'][0]['funding'],-2.)
    def test_binance_missing_settlement_is_unknown(self):
        p=self.position();self.assertFalse(self.p.funding(p,{'binance:perp:BTC':feed(START+10000)},START+10000))
        self.assertEqual(p['funding_cursor'],START)
    def test_kraken_funding_prorated_by_hour_no_8h_assumption(self):
        p=self.position('kraken');d=feed(START+1800000,rate=3.)
        hour=START//3600000*3600000;d['rates']={str(hour):3.,str(hour+3600000):3.}
        self.p.funding(p,{'kraken:perp:BTC':d},START+1800000)
        self.assertAlmostEqual(p['legs'][0]['funding'],3.)
    def test_pair_pnl_both_legs_fees_and_margin_not_returned_twice(self):
        a=q(price=100);s=q('upbit','spot',80);books={a['id']:a,s['id']:s};f={a['id']:feed()}
        self.p.reserve('pair','BASIS',s,a,100,fx(),START,dict(mean=0))
        for x in books.values():x.update(ts=START+1000,exchange_ts=START+1000)
        self.p.enter_pending(books,fx(),f,START+1000)
        p=self.p.s['positions']['pair'];p['funding_valid']=True
        self.p.close_position(p,books,fx(),START+2000,'TEST')
        pnl=self.p.db.execute('SELECT pnl FROM model_trades').fetchone()[0]
        self.assertLess(pnl,0)
        self.assertAlmostEqual(sum(w['cash'] for w in self.p.s['wallets'].values()),INITIAL+pnl)
        self.assertFalse(self.p.s['positions'])
    def test_restart_keeps_initial_and_wallets(self):
        self.p.save(START);before=copy.deepcopy(self.p.s);self.p.db.close()
        self.p=Derivatives(Path(self.tmp.name)/'derivatives.sqlite3',START+100000)
        self.assertEqual(self.p.s['started_ms'],START);self.assertEqual(self.p.s['wallets'],before['wallets'])
    def test_stale_fx_marks_unknown(self):
        self.assertEqual(self.p.value({}, {'KRW':fx()['KRW']}, START),(None,False))
    def test_depth_and_lot_rounding(self):
        with self.assertRaises(ValueError):fill(q(),'BUY',101)
        self.assertEqual(quantize(.129,[.001,.01]),.12)
    def test_directional_short_profit_wallet_reconciliation(self):
        a=q();self.p.reserve('s','SHORT',None,a,10,fx(),START,{})
        a.update(ts=START+1000,exchange_ts=START+1000)
        self.p.enter_pending({a['id']:a},fx(),{a['id']:feed()},START+1000,dict(state='DOWN',ts=START))
        p=self.p.s['positions']['s'];lower=q(price=97,ts=START+2000)
        self.p.close_position(p,{a['id']:lower},fx(),START+2000,'PROFIT')
        pnl=self.p.db.execute('SELECT pnl FROM model_trades').fetchone()[0]
        self.assertGreater(pnl,0)
        self.assertAlmostEqual(sum(w['cash'] for w in self.p.s['wallets'].values()),INITIAL+pnl)


class BearTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.p=BearPaper(Path(self.tmp.name)/'bear.sqlite3',START)
    def tearDown(self):self.p.db.close();self.tmp.cleanup()
    def test_independent_3m_and_persistent_account(self):
        self.assertEqual(self.p.s['cash'],INITIAL);self.assertEqual(self.p.policy['version'],'fast-bear-20261009-v1')
    def test_flow_resumes_after_assessment_prunes_old_bucket(self):
        self.p.flow['KRW-BTC']={}
        self.p.trade_side('KRW-BTC',100,2,START,'BID')
        self.assertEqual(self.p.flow['KRW-BTC'][START//FIVE*FIVE],[200,200,1])
    def test_confirm_next_bar_and_regime_gate(self):
        symbol='KRW-BTC';bucket=START//FIVE*FIVE
        f=dict(previous_rsi=29,rsi=32,close=100,vwap=99,low=99,value=1000,prior_value=500)
        self.p.regime=dict(state='DOWN',ts=START)
        self.p.history[symbol]=[[bucket,100,101,99,100,10,1000]]
        self.p.flow[symbol][bucket]=[1000,700,30]
        with patch('magi2.fast_models.bear.features',return_value=f):self.p.assess(symbol,bucket,START)
        self.assertFalse(self.p.s['pending']);self.assertIn(symbol,self.p.watch)
        self.p.regime['ts']=START+FIVE
        self.p.history[symbol].append([bucket+FIVE,100,101,99.5,100,10,1000])
        self.p.flow[symbol][bucket+FIVE]=[1000,700,30]
        with patch('magi2.fast_models.bear.features',return_value=dict(f,previous_rsi=32,low=99.5)):
            self.p.assess(symbol,bucket+FIVE,START+FIVE)
        self.assertIn(symbol,self.p.s['pending'])
        self.p.on_book(symbol,START+FIVE+1000,dict(bp=100,ap=100.01,stamp=START+FIVE+1000,bids=[(100,100)],asks=[(100.01,100)]))
        self.assertFalse(self.p.s['positions']) # no timely post-signal trade
    def test_gap_and_insufficient_candles_no_features(self):
        rows=[[i*FIVE,100,101,99,100,10,1000] for i in range(31)]
        self.assertIsNotNone(features(rows));self.assertIsNone(features(rows[:10]+rows[11:]))
    def test_old_fast_stop_does_not_pause_new_bear(self):
        o=Observer(Path(self.tmp.name)/'observer.sqlite3',START)
        o.models=type('M',(),{'bear':self.p})();o.disable_fast(START)
        self.assertTrue(o.fast_disabled);self.assertFalse(self.p.s.get('entries_paused',False));o.db.close()
    def test_readonly_report_creates_nothing(self):
        root=Path(self.tmp.name)/'absent';text,_=view(root,START)
        self.assertIn('원장 준비 중',text);self.assertFalse(root.exists())

if __name__=='__main__':unittest.main()
