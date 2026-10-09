import copy
import json
from pathlib import Path
import tempfile
import unittest

from magi1.fast_diagnostics import ledger_review
from magi1.fast_observe import Observer, DAY
from magi1.fast_paper import Paper
from magi1.fast_performance import daily_performance
from magi2.fast_flow_paper_report import view as account_view
from magi2.fast_observe_service import Service, view as watch_view
from magi2.paper_performance import fast_results, view as results_view, daily_rows

T = 200*DAY


class PerformanceResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.path = self.root/'fast-observe/paper-v1.sqlite3'
        self.p = Paper(self.path, T); self.p.diagnostics = True
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        self.p.close(T+3*DAY)

    def book(self, ts, bid, size=100000):
        return dict(bp=bid, ap=bid+.01, ts=ts, stamp=ts, bids=[(bid,size)], asks=[(bid+.01,size)])

    def buy(self, symbol, ts):
        self.p.signal(symbol, ts, 100, 95, {})
        self.p.on_trade(symbol, ts+1, 100, 1, ts+1)
        self.p.on_book(symbol, ts+2, self.book(ts+2, 100))

    def old_loss_and_resume(self):
        self.buy('KRW-OLD', T+1)
        self.p.request_exit('KRW-OLD', T+4, 'TEST')
        self.p.on_book('KRW-OLD', T+5, self.book(T+5, 96))
        self.p.tick(T+10000)
        self.p.pause_entries(T+20000)
        self.start = T+30000
        self.p.resume_entries(self.start, 'review')
        self.base = self.p.s['cash']

    def test_all_views_start_at_zero_without_rewriting_original_account(self):
        self.old_loss_and_resume()
        before = copy.deepcopy(self.p.s)
        fills = self.p.db.execute('SELECT * FROM fills').fetchall()
        report = self.p.report(self.start+1)
        self.assertEqual((report['closed'],report['winning'],report['realized'],report['return_pct']), (0,0,0,0))
        self.assertEqual(report['initial'], self.base)
        self.assertEqual(report['started_ms'], self.start)
        result = fast_results(self.root, self.start+1)
        self.assertTrue(result.wins_complete); self.assertFalse(result.trades)
        self.assertEqual((result.initial,result.started), (self.base,self.start))
        self.assertEqual(daily_rows(result,self.start+1)[0][2], '+0.00%')
        text = results_view(self.root,self.start+1,'fast')[0]
        self.assertIn('청산 0건', text); self.assertIn('수익률 +0.00%', text)
        text = account_view(self.root)[0]
        self.assertIn('완료매매 0회',text); self.assertNotIn('조기 진입 비교실험',text)
        orders = account_view(self.root,'fast_paper_orders')[0]
        self.assertNotIn('KRW-OLD',orders); self.assertIn('완료 0회',orders)
        daily = account_view(self.root,'fast_paper_daily')[0]
        self.assertIn('첫 평가 기록 대기', daily); self.assertNotIn('실현 누계', daily)
        self.assertEqual(ledger_review(self.p)['completed'],0)
        self.assertEqual(ledger_review(self.p)['diagnostics_closed'],0)
        self.assertEqual(before,self.p.s)
        self.assertEqual(fills,self.p.db.execute('SELECT * FROM fills').fetchall())
        self.assertFalse(self.p.can_signal('KRW-OLD',self.start+1))

    def test_new_partial_trade_counts_once_and_survives_restart(self):
        self.old_loss_and_resume()
        self.buy('KRW-NEW',self.start+1)
        qty = self.p.s['positions']['KRW-NEW']['qty']
        self.p.request_exit('KRW-NEW',self.start+4,'TEST')
        self.p.on_book('KRW-NEW',self.start+5,self.book(self.start+5,104,qty/2))
        self.assertFalse(fast_results(self.root,self.start+6).trades)
        end = T+DAY+1
        self.p.on_book('KRW-NEW',end,self.book(end,102))
        self.p.tick(end+10000)
        result = fast_results(self.root,end+10001)
        self.assertTrue(result.wins_complete)
        self.assertEqual(len(result.trades),1); self.assertGreater(result.trades[0].pnl,0)
        self.assertEqual(self.p.s['closed'],2)
        self.assertEqual((self.p.report(end)['closed'],self.p.report(end)['winning']), (1,1))
        expected = (self.p.s['cash']/self.base-1)*100
        self.assertAlmostEqual(self.p.report(end)['return_pct'],expected)
        self.assertEqual(ledger_review(self.p)['completed'],1)
        self.p.close(end+10002); self.p = Paper(self.path,end+10003)
        self.assertEqual(self.p.report(end+10003)['started_ms'],self.start)
        self.assertEqual(fast_results(self.root,end+10004).trades,result.trades)
        self.assertEqual(self.p.report(end+10004)['closed'],1)

    def test_pre_deploy_daily_payload_is_rebased_once(self):
        self.old_loss_and_resume()
        legacy = dict(equity=self.base, realized=self.p.s['realized'], return_pct=-2.6)
        converted = daily_performance(legacy, self.p.s)
        self.assertEqual((converted['return_pct'], converted['realized']), (0,0))
        current = self.p.report(self.start+1)
        self.assertEqual(daily_performance(current,self.p.s)['realized'],0)
        self.p.db.execute('INSERT OR REPLACE INTO daily VALUES(?,?,?)',(T//DAY,self.start+1,json.dumps(dict(legacy,stale_marks=0))))
        self.p.db.commit()
        text = account_view(self.root,'fast_paper_daily')[0]
        self.assertIn('누적 +0.00%',text); self.assertIn('실현 누계 +0원',text)

    def test_capture_counts_and_watch_hide_old_outcomes_without_deleting(self):
        self.old_loss_and_resume()
        service = Service(self.root, lambda x:None)
        o = Observer(service.db, T); o.paper = self.p
        for stamp,symbol in [(T+1,'KRW-OLD'),(self.start+1,'KRW-NEW')]:
            cur=o.db.execute('INSERT INTO captures(ts,symbol,day,price,payload) VALUES(?,?,?,?,?)',(stamp,symbol,T//DAY,100,'{}'))
            o.db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?)',(cur.lastrowid,60,'OBSERVED',stamp+60000,101,1.))
        o.db.commit()
        report=o.report()
        self.assertEqual(report['captures'],1); self.assertEqual(report['outcomes'],{'OBSERVED':1})
        service.save(phase='OBSERVING',observation=report)
        text=watch_view(self.root)
        self.assertIn('KRW-NEW',text); self.assertNotIn('KRW-OLD',text)
        self.assertEqual(o.db.execute('SELECT count(*) FROM captures').fetchone()[0],2)
        o.db.close(); service.stop()

    def test_without_restart_anchor_legacy_account_keeps_original_basis(self):
        before=copy.deepcopy(self.p.s)
        report=self.p.report(T+1)
        self.assertEqual(report['performance_scope'],'LIFETIME')
        self.assertEqual(report['initial'],3000000)
        self.assertEqual(report['started_ms'],T)
        self.assertEqual(before,self.p.s)


if __name__ == '__main__': unittest.main()
