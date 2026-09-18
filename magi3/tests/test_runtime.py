import json
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch,Mock
from magi3.config import Config
from magi3.runtime_store import RuntimeStore
from magi3.shadow_runtime import ShadowEngine,stamp
from magi3.market_data import make_book
from magi2.shadow_bridge import Outbox,candidates
from datetime import datetime,timezone


class Market:
    def __init__(self):self.bids=[(99,10000)];self.asks=[(100,10000)];self.fail=False
    def book(self,venue,asset,fresh=False):
        if self.fail:raise RuntimeError('quote unavailable')
        return make_book(venue,asset,'KRW-'+asset,self.bids,self.asks,1,'test')
    def mark(self,venue,asset):
        b=self.book(venue,asset)
        return {'price_krw':b.mid,'received_ts_ms':b.received_ms,'source':'test'}


def intent(ident='test:1',amount=10000):
    now=stamp()
    return {'id':ident,'source':'MAGI2','mode':'SHADOW','side':'BUY','venue':'upbit','asset':'BTC',
            'created_ts_ms':now,'expires_ts_ms':now+120000,'notional_krw':amount,
            'max_holding_seconds':1,'strategy_tags':['VPD']}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=RuntimeStore(self.temp.name)
        self.market=Market();self.cfg=Config('SHADOW',False,False,30000,30000,10000)
        self.engine=ShadowEngine(self.store,self.market,self.cfg)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def test_fill_restart_duplicate_and_exit_accounting(self):
        s=intent();self.assertEqual(self.engine.ingest(s),'FILLED')
        self.assertAlmostEqual(self.store.get('cash'),89990)
        self.store.close();self.store=RuntimeStore(self.temp.name,999999)
        self.engine=ShadowEngine(self.store,self.market,self.cfg)
        self.engine.ingest(s);self.assertEqual(len(self.store.recent_orders()),1)
        deadline=self.store.positions()[0]['deadline_ms']
        self.assertEqual(self.engine.exit_due(deadline),['CLOSED'])
        self.assertEqual(self.store.positions(),[])
        self.assertAlmostEqual(self.store.get('cash'),99880.1)
        report=self.engine.report()
        self.assertAlmostEqual(report['realized_pnl_krw'],-119.9)
        self.assertAlmostEqual(report['fees_krw'],19.9)
        self.engine.exit_due(deadline+1);self.assertEqual(len(self.store.recent_orders()),2)
    def test_partial_buy_partial_exit_and_retry(self):
        self.market.asks=[(100,10)]
        self.assertEqual(self.engine.ingest(intent()),'PARTIAL_IOC')
        self.assertAlmostEqual(self.store.positions()[0]['cost'],1001)
        deadline=self.store.positions()[0]['deadline_ms'];self.market.bids=[(99,4)]
        self.assertEqual(self.engine.exit_due(deadline),['PARTIAL_IOC'])
        self.assertAlmostEqual(self.store.positions()[0]['qty'],6)
        self.assertAlmostEqual(self.store.positions()[0]['cost'],600.6)
        self.market.fail=True;self.assertEqual(self.engine.exit_due(deadline+1),['EXIT_PENDING_BOOK'])
        self.market.fail=False;self.market.bids=[(99,20)]
        self.engine.exit_due(deadline+2)
        self.assertEqual(self.store.positions(),[])
        self.assertAlmostEqual(self.engine.report()['realized_pnl_krw'],-11.99)
    def test_pending_recovered_after_quote_failure(self):
        self.market.fail=True;s=intent()
        self.assertEqual(self.engine.ingest(s),'PENDING_BOOK')
        self.market.fail=False;self.engine.recover_pending()
        self.assertEqual(len(self.store.positions()),1)
    def test_expired_pending_cannot_be_refreshed_by_same_id(self):
        self.market.fail=True;s=intent();self.engine.ingest(s)
        with self.store.transaction() as db:
            s['expires_ts_ms']=stamp()-1;s['created_ts_ms']=stamp()-120000
            db.execute('UPDATE signals SET payload=?',(json.dumps(s),))
        self.market.fail=False;self.engine.ingest(intent())
        self.assertEqual(self.store.positions(),[])
        self.assertEqual(self.store.rows('SELECT status FROM signals')[0]['status'],'REJECTED')
    def test_limits_fees_kill_and_exit_remains_enabled(self):
        self.assertEqual(self.engine.ingest(intent(amount=30001)),'MAX_ORDER')
        self.engine.ingest(intent('test:2',20000))
        self.assertEqual(self.engine.ingest(intent('test:3',10000)),'EXPOSURE_LIMIT')
        self.engine.config=replace(self.cfg,kill_switch=True)
        self.assertEqual(self.engine.ingest(intent('test:4')),'KILL_SWITCH')
        self.assertEqual(self.engine.exit_due(self.store.positions()[0]['deadline_ms']),['CLOSED'])
    def test_daily_realized_loss_gate_and_unknown_valuation(self):
        self.engine.config=replace(self.cfg,max_daily_loss_krw=1)
        self.engine.ingest(intent());self.engine.exit_due(self.store.positions()[0]['deadline_ms'])
        self.assertEqual(self.engine.ingest(intent('test:2')),'DAILY_REALIZED_LOSS')
    def test_unknown_mark_is_not_zero(self):
        self.engine.ingest(intent());self.market.fail=True
        r=self.engine.report();self.assertIsNone(r['equity_krw']);self.assertIsNone(r['unrealized_pnl_krw'])
    def test_invalid_and_live_fail_closed(self):
        for change in ({'source':'MAGI1'},{'mode':'LIVE'},{'side':'SELL'},{'notional_krw':-1},{'expires_ts_ms':0}):
            s=intent();s.update(change);self.assertEqual(self.engine.ingest(s),'INVALID_OR_EXPIRED')
        with self.assertRaises(ValueError):self.engine.ingest({**intent(),'notional_krw':float('nan')})
        with self.assertRaises(ValueError):ShadowEngine(self.store,self.market,replace(self.cfg,mode='LIVE'))
        with self.assertRaises(ValueError):ShadowEngine(self.store,self.market,replace(self.cfg,live_enabled=True))
        self.assertEqual(self.store.recent_orders(),[])
    def test_transaction_rollback_and_monotone_nonce(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                db.execute("UPDATE meta SET value='0' WHERE key='cash'");raise RuntimeError()
        self.assertEqual(self.store.get('cash'),100000)
        a=self.store.nonce();self.store.close();self.store=RuntimeStore(self.temp.name)
        self.assertGreater(self.store.nonce(),a)


class OutboxTests(unittest.TestCase):
    def snapshot(self):
        return {'asof':datetime.now(timezone.utc).isoformat(),'universe':'UPBIT_KRW',
                'top10':[{'coin':'BTC','market':'KRW-BTC','VPD':80}]}
    def test_no_refresh_across_restart(self):
        with tempfile.TemporaryDirectory() as root:
            box=Outbox(root);now=stamp();snap=self.snapshot()
            box.update(snap,now);original=box.report()['signals'][0]
            box.store.close();box=Outbox(root);box.update(snap,now+50000)
            self.assertEqual(box.report()['signals'][0],original)
            box.store.close()
    def test_stale_source_and_score_not_probability(self):
        snap=self.snapshot();s=candidates(snap,stamp())[0]
        self.assertIsNone(s['expected_move_bps']);self.assertEqual(s['score'],80)
        with self.assertRaises(ValueError):candidates(snap,stamp()+13*3600000)
        snap['top10'][0]['VPD']=float('nan');self.assertEqual(candidates(snap,stamp()),[])
