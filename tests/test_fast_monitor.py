import tempfile
import unittest
from unittest.mock import Mock,patch
from pathlib import Path
from magi2.fast_monitor import Watch,FastMonitor
from magi2.fast_lab.rank_tracker import RankPolicy,RankTracker
from magi3.fast_execution import FastBudget,UpbitFastOrders
from magi2.telegram_ui import main_keyboard,role_keyboard,parse_command

class FastMonitorTests(unittest.TestCase):
    def test_five_minute_scan_and_fifteen_minute_watch(self):
        t=RankTracker(RankPolicy(scan_minutes=5,watch_minutes=15))
        t.snapshot('upbit',0,{'KRW-A':100})
        self.assertEqual(t.scan('upbit',0)['status'],'WARMING_UP')
        t.snapshot('upbit',300000,{'KRW-A':102})
        r=t.scan('upbit',300000)
        self.assertEqual(r['watch_until_ms'],1200000)
        self.assertEqual(r['watchlist'][0]['symbol'],'KRW-A')
        self.assertIsNone(t.scan('upbit',301000))
    def test_breakout_once_fresh_only_not_chasing(self):
        row={'price':100}
        w=Watch(row,0)
        for ts in range(0,30000,1000):self.assertFalse(w.quote(ts,100,100.01))
        self.assertTrue(w.quote(30000,100.1,100.11))
        self.assertFalse(w.quote(31000,100.2,100.21))
        gap=Watch(row,0)
        for ts in range(0,30000,1000):gap.quote(ts,100,100.01)
        self.assertFalse(gap.quote(35000,100.1,100.11))
        wide=Watch(row,0)
        for ts in range(0,30000,1000):wide.quote(ts,100,100.01)
        self.assertFalse(wide.quote(30000,100.1,102))
        self.assertFalse(wide.quote(31000,103,103.01))
    def test_button_removed_legacy_status_kept(self):
        self.assertNotIn('FAST 후보',str(main_keyboard()))
        self.assertNotIn('FAST 후보',str(role_keyboard()))
        self.assertEqual(parse_command('⚡ FAST 후보'),'fast')
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        m=FastMonitor(temporary.name,lambda _:None);self.addCleanup(m.audit.db.close)
        self.assertIn('실주문 OFF',m.summary())
        for i in range(10):m.events.put({'text':str(i)})
        self.assertEqual(len(m.drain()),4)

class FastBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.b=FastBudget(Path(self.temp.name)/'fast.db')
        self.snap={'complete':True,'observed_ts_ms':1000,'total_equity_krw':1000000,
                   'available_cash_krw':100000,'fast_position_value_krw':0,'external_fast_pending_krw':0}
    def tearDown(self):self.b.close();self.temp.cleanup()
    def test_strict_boundary_fees_existing_and_pending(self):
        with self.assertRaisesRegex(ValueError,'5_PERCENT'):self.b.reserve('a',{},self.snap,50000,0,1000)
        ident,new=self.b.reserve('a',{},self.snap,30000,.001,1000)
        self.assertTrue(new)
        self.assertEqual(self.b.reserve('a',{},self.snap,30000,.001,1000),(ident,False))
        self.b.state(ident,'UNKNOWN')
        with self.assertRaisesRegex(ValueError,'5_PERCENT'):self.b.reserve('b',{},self.snap,20000,0,1000)
        self.b.close();self.b=FastBudget(Path(self.temp.name)/'fast.db')
        with self.assertRaisesRegex(ValueError,'5_PERCENT'):self.b.reserve('b',{},self.snap,20000,0,1000)
    def test_incomplete_stale_or_no_cash_blocks(self):
        for fields in ({'complete':False},{'observed_ts_ms':-10000},{'available_cash_krw':0},{'fast_position_value_krw':50000}):
            with self.assertRaises(ValueError):self.b.reserve('s',{},dict(self.snap,**fields),10000,.001,1000)
    def test_live_default_off_and_timeout_never_resubmits(self):
        adapter=Mock();adapter._auth.return_value={};adapter.http.post.side_effect=TimeoutError
        client=UpbitFastOrders(adapter,self.b)
        self.assertEqual(client.buy('s','KRW-BTC',100,10000,self.snap,.001,5000)['status'],'BLOCKED')
        adapter.http.post.assert_not_called()
        client.enabled=True
        with patch('magi3.fast_execution.time.time_ns',return_value=1000000000):
            first=client.buy('s','KRW-BTC',100,10000,self.snap,.001,5000)
            second=client.buy('s','KRW-BTC',100,10000,self.snap,.001,5000)
        self.assertEqual(first['status'],'UNKNOWN');self.assertEqual(second['status'],'EXISTING')
        adapter.http.post.assert_called_once()
