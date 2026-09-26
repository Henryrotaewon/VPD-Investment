import json
from pathlib import Path
import tempfile
import unittest

from magi1.fast_paper import Paper, POLICY, DAY
from magi1.fast_observe import Observer, FIVE
from magi2.fast_flow_paper_report import view
from magi2.telegram_ui import parse_command


class FastPaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'fast-observe'/'paper-v1.sqlite3'
        self.start = 30*DAY
        self.p = Paper(self.path, self.start)

    def tearDown(self):
        self.p.close(self.start+1000000)
        self.tmp.cleanup()

    def book(self, t, bp=99.99, ap=100, size=100000, stamp=None):
        return dict(bp=bp, ap=ap, bq=size, aq=size, ts=t,
                    stamp=t if stamp is None else stamp,
                    bids=[(bp,size)], asks=[(ap,size)])

    def buy(self, symbol='KRW-X', t=None):
        t = t or self.start+1
        self.p.signal(symbol,t,100,98,{})
        self.p.on_trade(symbol,t+1,100,10,t+1)
        self.p.on_book(symbol,t+2,self.book(t+2))
        return self.p.s['positions'][symbol]

    def test_ten_slots_include_pending_and_reinvest_cash(self):
        t = self.start+1
        for n in range(11): self.p.signal(f'KRW-{n}',t,100,98,{})
        self.assertEqual(len(self.p.s['pending']),10)
        self.assertEqual(sum(o['budget'] for o in self.p.s['pending'].values()),3000000)
        self.assertEqual(self.p.s['cash'],3000000)
        self.assertEqual(self.p.db.execute("SELECT reason FROM signals WHERE symbol='KRW-10'").fetchone()[0],'NO_SLOT')
        for n in range(10):
            self.p.on_trade(f'KRW-{n}',t+1,100,1,t+1)
            self.p.on_book(f'KRW-{n}',t+2,self.book(t+2))
        self.assertEqual(len(self.p.s['positions']),10)
        self.assertAlmostEqual(self.p.s['cash'],0)
        self.p.request_exit('KRW-0',t+3,'TEST')
        self.p.on_book('KRW-0',t+4,self.book(t+4,bp=105,ap=105.01))
        cash = self.p.s['cash']
        self.assertGreater(cash,300000)
        self.p.signal('KRW-NEW',t+5,100,98,{})
        self.assertAlmostEqual(self.p.s['pending']['KRW-NEW']['budget'],cash)

    def test_no_old_quote_fill_cap_depth_and_expiry(self):
        t = self.start+1
        self.p.signal('KRW-X',t,100,98,{})
        self.p.on_trade('KRW-X',t,100,1,t)
        self.p.on_book('KRW-X',t,self.book(t))
        self.p.on_book('KRW-X',t+1,self.book(t+1,stamp=t-3000))
        self.assertFalse(self.p.s['positions'])
        self.p.on_book('KRW-X',t+2,self.book(t+2,size=1))
        self.assertEqual(self.p.s['pending']['KRW-X']['last_reason'],'INSUFFICIENT_VISIBLE_DEPTH')
        self.p.on_book('KRW-X',t+3,self.book(t+3,bp=101,ap=102))
        self.assertFalse(self.p.s['positions'])
        self.p.tick(t+10001)
        self.assertFalse(self.p.s['pending'])
        self.assertEqual(self.p.s['cash'],3000000)
        self.assertEqual(self.p.db.execute('SELECT reason FROM signals').fetchone()[0],'ENTRY_PRICE_CAP')

    def test_depth_costs_partial_exit_and_no_reused_quote(self):
        p = self.buy(); t = self.start+100
        initial_qty = p['qty']
        self.assertAlmostEqual(p['cost'],300000)
        self.assertAlmostEqual(initial_qty,300000/(100*1.0005*1.0005))
        self.p.on_trade('KRW-X',t,97,1,t)
        # Stop price is not a guaranteed fill; next visible bid determines it.
        self.p.on_book('KRW-X',t+1,self.book(t+1,bp=96,ap=96.1,size=100))
        self.assertAlmostEqual(p['qty'],initial_qty-100)
        self.p.on_book('KRW-X',t+2,self.book(t+2,bp=96,ap=96.1,size=100,stamp=t+1))
        self.assertAlmostEqual(p['qty'],initial_qty-100)
        self.p.on_book('KRW-X',t+3,self.book(t+3,bp=95,ap=95.1))
        self.assertFalse(self.p.s['positions'])
        self.assertEqual(self.p.s['closed'],1)
        self.assertLess(self.p.s['realized'],0)
        self.assertAlmostEqual(self.p.s['cash'],3000000+self.p.s['realized'])
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0],2)

    def test_restart_preserves_account_cancels_pending_and_exits_after_fresh_quote(self):
        self.buy()
        self.p.signal('KRW-Y',self.start+4,100,98,{})
        self.p.close(self.start+5)
        self.p = Paper(self.path,self.start+50000)
        self.assertEqual(self.p.s['cash'],2700000)
        self.assertFalse(self.p.s['pending'])
        self.assertTrue(self.p.s['positions']['KRW-X']['uncertain'])
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM fills WHERE side='SELL'").fetchone()[0],0)
        t = self.start+50001
        self.p.on_book('KRW-X',t,self.book(t,bp=95,ap=95.1))
        self.assertFalse(self.p.s['positions'])
        self.assertEqual(self.p.s['closed'],1)
        self.p.signal('KRW-X',t+1,100,98,{})
        self.assertFalse(self.p.s['pending'])

    def test_gap_only_affects_related_symbol(self):
        self.buy('KRW-X'); self.buy('KRW-Y')
        self.p.signal('KRW-Z',self.start+20,100,98,{})
        self.p.gap(['KRW-X','KRW-Z'],self.start+30,'CONNECTION')
        self.assertTrue(self.p.s['positions']['KRW-X']['exit'])
        self.assertIsNone(self.p.s['positions']['KRW-Y']['exit'])
        self.assertFalse(self.p.s['pending'])

    def test_flow_exit_two_full_windows_below_vwap(self):
        p = self.buy(); p['value']=200; p['volume']=2
        for offset in (20000,30000):
            t = self.start+offset
            self.p.on_trade('KRW-X',t-1,99,1,t-1)
            self.p.window('KRW-X',t,[dict(t=t-10000,value=100,buy=20,l=99)])
            if offset==20000: self.assertIsNone(p['exit'])
        self.assertEqual(p['exit']['reason'],'NET_SELL_20S_BELOW_VWAP')

    def test_time_exit_and_trailing_never_lower_stop(self):
        p = self.buy()
        for n,low in ((2,99),(3,99.1),(4,99.2),(5,98.5)):
            end = self.start+n*60000
            bars = [dict(t=end-60000+i*10000,value=1,buy=1,l=low) for i in range(6)]
            self.p.window('KRW-X',end,bars)
            if n>=4: self.assertAlmostEqual(p['stop'],99)
        t = self.start+300001
        self.p.on_book('KRW-X',t,self.book(t))
        self.assertEqual(p['exit']['reason'],'NO_NEW_HIGH_3M_NONPOSITIVE')
        self.assertIn('KRW-X',self.p.s['positions'])
        self.p.on_book('KRW-X',t+1,self.book(t+1))
        self.assertFalse(self.p.s['positions'])

    def test_no_historical_import_and_daily_readonly_views(self):
        self.p.signal('KRW-PAST',self.start-1,100,98,{})
        self.assertFalse(self.p.s['pending'])
        self.p.tick(self.start+10000)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM daily').fetchone()[0],1)
        for section in ('fast_paper','fast_paper_orders','fast_paper_daily'):
            text,keyboard = view(self.tmp.name,section)
            self.assertIn('[PAPER]',text)
            self.assertIn('fast_paper_orders',str(keyboard))
        self.assertEqual(parse_command('⚡ FAST 모의투자'),'fast_paper')
        self.assertEqual(parse_command('/fast'),'fast')  # legacy indicator alias stays intact

    def test_observer_only_hands_off_new_capture_with_full_protection_history(self):
        o = Observer(Path(self.tmp.name)/'obs.db',self.start)
        o.paper = self.p
        o.connected('a',['KRW-X'],self.start)
        for k in range(1,11):
            o.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-X',self.start-k*DAY,100))
        o.db.execute('INSERT INTO baseline VALUES(?,?,?)',('KRW-X',self.start,300))
        o.current=self.start+FIVE+60000
        for i in range(6):
            o.bars['KRW-X'].append(dict(t=o.current-60000+i*10000,value=1,buy=1,count=1,ofi=1,l=98))
        for amount in (30,90):
            o.bar('KRW-X').update(value=amount,buy=amount,count=20,ofi=1,l=99)
            end=o.current+10000
            o.books['KRW-X']=dict(ts=end-1,bp=99.99,ap=100)
            o.last_price['KRW-X']=(end-1,100,end-1)
            o.advance(end+1)
        self.assertEqual(o.report()['captures'],1)
        self.assertEqual(len(self.p.s['pending']),1)
        self.assertEqual(self.p.s['pending']['KRW-X']['stop'],98)
        self.assertFalse(self.p.s['positions'])
        o.db.close()


if __name__ == '__main__':
    unittest.main()
