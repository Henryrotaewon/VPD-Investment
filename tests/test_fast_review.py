import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from magi1.fast_diagnostics import acceleration, ledger_review
from magi1.fast_experiment import Experiment
from magi1.fast_observe import Observer, DAY
from magi1.fast_paper import Paper
from magi2.fast_flow_paper_report import view
from magi2.fast_observe_service import Service

T = 200*DAY


def book(ts, bid=100, ask=100.1, size=100000):
    return dict(bp=bid, ap=ask, ts=ts, stamp=ts, bids=[(bid, size)], asks=[(ask, size)])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'fast-observe/paper-v1.sqlite3'
        self.paper = Paper(self.path, T)
        self.paper.diagnostics = True

    def tearDown(self):
        self.paper.close(T+DAY)
        self.tmp.cleanup()

    def buy(self):
        self.paper.signal('KRW-X', T+1, 100, 98, dict(price_acceleration_bps=5,
                          confirmation_rise_bps=10, confirmation_delay_ms=10000))
        self.paper.on_trade('KRW-X', T+2, 100, 1, T+2)
        self.paper.on_book('KRW-X', T+3, book(T+3))

    def test_resume_preserves_history_and_later_stop_wins_across_restart(self):
        self.buy()
        self.paper.request_exit('KRW-X', T+10, 'TEST')
        self.paper.on_book('KRW-X', T+11, book(T+11, bid=99, ask=99.1))
        o = Observer(self.root/'obs.db', T)
        o.paper = self.paper
        o.experiment = Experiment(self.root/'exp', T)
        o.disable_fast(T+12)
        before = {k:copy.deepcopy(self.paper.s[k]) for k in ('cash','realized','closed','winning','started_ms','policy')}
        fills = self.paper.db.execute('SELECT * FROM fills').fetchall()
        self.assertTrue(o.resume_fast(T+13, 'approved-once'))
        self.assertEqual(before, {k:self.paper.s[k] for k in before})
        self.assertEqual(fills, self.paper.db.execute('SELECT * FROM fills').fetchall())
        self.assertFalse(self.paper.can_signal('KRW-X', T+14))
        self.assertTrue(self.paper.can_signal('KRW-Y', T+14))
        self.assertTrue(all(p.s['entries_paused'] for p in o.experiment.accounts.values()))
        self.assertEqual(self.paper.report(T+14)['review_segment']['return_pct'], 0)
        self.assertIn('재개 후', view(self.root)[0])
        o.disable_fast(T+15)
        self.assertFalse(o.resume_fast(T+16, 'approved-once'))
        self.assertTrue(o.fast_disabled)
        for p in o.experiment.accounts.values(): p.close(T+16)
        o.db.close()
        o = Observer(self.root/'obs.db', T+17); o.paper = self.paper
        self.assertFalse(o.resume_fast(T+17, 'approved-once'))
        self.assertTrue(o.fast_disabled); self.assertTrue(self.paper.s['entries_paused'])
        self.assertTrue(o.resume_fast(T+18, 'new-approval'))
        o.db.close()

    def test_resume_rejects_nonflat_account_without_enabling_observer(self):
        self.buy()
        o = Observer(self.root/'obs.db', T); o.paper = self.paper
        o.disable_fast(T+4)
        with self.assertRaisesRegex(ValueError, 'FLAT'):
            o.resume_fast(T+5, 'new')
        self.assertTrue(o.fast_disabled); self.assertTrue(self.paper.s['entries_paused'])
        self.assertIn('KRW-X', self.paper.s['positions'])
        o.db.close()

    def test_depth_cost_extremes_partial_exit_and_gap_do_not_change_accounting(self):
        self.buy()
        p = self.paper.s['positions']['KRW-X']
        original = p['original_cost']
        self.assertLess(p['diagnostic']['immediate_exit_net_pct'], -.2)
        self.paper.on_book('KRW-X', T+4, book(T+4, bid=104, ask=104.1))
        peak = p['diagnostic']['best_exit_net_pct']
        self.assertGreater(peak, 3)
        self.paper.gap(['KRW-X'], T+5, 'TEST')
        self.paper.on_book('KRW-X', T+6, book(T+6, bid=97, ask=97.1, size=10))
        self.assertGreater(p['diagnostic']['depth_missing_books'], 0)
        self.paper.on_book('KRW-X', T+7, book(T+7, bid=96, ask=96.1))
        self.assertFalse(self.paper.s['positions'])
        evidence = json.loads(self.paper.db.execute("SELECT payload FROM events WHERE kind='EXIT_DIAGNOSTIC'").fetchone()[0])
        self.assertFalse(evidence['continuous'])
        self.assertEqual(evidence['best_exit_net_pct'], peak)
        self.assertAlmostEqual(evidence['net_return_pct'], self.paper.s['realized']/original*100)
        self.assertAlmostEqual(self.paper.s['cash'], 3000000+self.paper.s['realized'])
        report = ledger_review(self.paper)
        self.assertEqual(report['completed'], 1)
        self.assertEqual(report['exit_reasons']['DATA_GAP_TEST']['trades'], 1)
        self.assertEqual(report['acceleration_cohorts']['GAP_POSITIVE']['trades'], 1)
        self.assertEqual(report['fill_delay_ms']['median'], 2)

    def test_post_exit_quotes_are_forward_only_and_do_not_change_cash(self):
        self.buy()
        self.paper.request_exit('KRW-X', T+4, 'TEST')
        closed = T+5
        self.paper.on_book('KRW-X', closed, book(closed))
        cash = self.paper.s['cash']; fills = self.paper.db.execute('SELECT count(*) FROM fills').fetchone()[0]
        self.paper.on_book('KRW-X', closed+59999, book(closed+59999, bid=110, ask=110.1))
        self.assertEqual(self.paper.db.execute("SELECT count(*) FROM events WHERE kind='POST_EXIT_DIAGNOSTIC'").fetchone()[0], 0)
        self.paper.on_book('KRW-X', closed+60001, book(closed+60001, bid=104, ask=104.1))
        row = json.loads(self.paper.db.execute("SELECT payload FROM events WHERE kind='POST_EXIT_DIAGNOSTIC'").fetchone()[0])
        self.assertEqual(row['status'], 'OBSERVED_QUOTE_PROXY')
        self.assertGreater(row['difference_pct'], 3)
        self.assertEqual(cash, self.paper.s['cash'])
        self.assertEqual(fills, self.paper.db.execute('SELECT count(*) FROM fills').fetchone()[0])
        self.paper.gap(['KRW-X'], closed+61000, 'TEST')
        self.assertFalse(self.paper.s['exit_reviews'])
        rows = [json.loads(x[0]) for x in self.paper.db.execute("SELECT payload FROM events WHERE kind='POST_EXIT_DIAGNOSTIC'")]
        self.assertEqual([r['status'] for r in rows], ['OBSERVED_QUOTE_PROXY','MISSING','MISSING'])

    def test_post_exit_late_or_shallow_book_never_invents_holding_return(self):
        self.buy(); self.paper.request_exit('KRW-X', T+4, 'TEST')
        closed = T+5; self.paper.on_book('KRW-X', closed, book(closed))
        self.paper.on_book('KRW-X', closed+60000, book(closed+60000, size=1))
        self.paper.tick(closed+310001)
        rows = [json.loads(x[0]) for x in self.paper.db.execute("SELECT payload FROM events WHERE kind='POST_EXIT_DIAGNOSTIC'")]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['status']=='MISSING' and 'hold_net_pct' not in r for r in rows))
        self.assertFalse(self.paper.s['exit_reviews'])

    def test_no_backfill_of_missing_historical_excursions(self):
        self.paper.diagnostics = False
        self.buy()
        self.paper.request_exit('KRW-X', T+4, 'TEST')
        self.paper.on_book('KRW-X', T+5, book(T+5))
        report = ledger_review(self.paper)
        self.assertEqual(report['completed'], 1)
        self.assertEqual(report['diagnostics_closed'], 0)
        self.assertEqual(report['initial_stop_distance_bps']['n'], 0)

    def test_new_fast_history_gate_does_not_cancel_bear_or_bollinger(self):
        o = Observer(self.root/'obs.db', T); o.paper = self.paper
        o.experiment = Experiment(self.root/'exp', T)
        bear = Mock(); bollinger = Mock()
        o.models = Mock(bear=bear); o.bollinger = bollinger
        o.connected('g', ['KRW-X'], T)
        o.repair_blocked.add('KRW-X')
        for p in [self.paper, *o.experiment.accounts.values()]:
            p.signal('KRW-X', T+1, 100, 98, {})
        ts = T+2
        o.ingest(dict(type='orderbook', code='KRW-X', timestamp=ts,
                     orderbook_units=[dict(bid_price=100, ask_price=100.1,bid_size=10000,ask_size=10000)]), ts)
        self.assertFalse(self.paper.s['pending'])
        bear.cancel.assert_not_called(); bollinger.cancel.assert_not_called()
        bear.on_book.assert_called_once(); bollinger.on_book.assert_called_once()
        for p in o.experiment.accounts.values(): p.close(ts)
        o.db.close()

    def test_service_selects_one_time_base_restart_and_repair(self):
        service = Service(self.root, lambda x:None)
        def worker(module, *args):
            self.assertIn('--repair', args); self.assertIn('--diagnostics', args)
            self.assertIn('--resume-fast-id', args); self.assertNotIn('--disable-fast', args)
            service.stopped.set()
            return 0
        service.worker = worker
        service.run()


class FeatureTests(unittest.TestCase):
    def test_completed_adjacent_windows_no_future_or_missing_price_imputation(self):
        bars = [dict(t=i*10000,c=100+i,l=99+i,value=10*(i+1),buy=8*(i+1),ofi=i) for i in range(6)]
        expected = acceleration(bars, 60000, book(59999))
        bars.append(dict(t=60000,c=100000,l=1,value=100000,buy=100000,ofi=100000))
        self.assertEqual(expected, acceleration(bars, 60000, book(59999)))
        self.assertLess(expected['price_acceleration_bps'], 0)
        self.assertAlmostEqual(expected['turnover_change_ratio'], 1.2)
        bars = [b for b in bars if b['t'] != 40000]
        missing = acceleration(bars, 60000, book(59999))
        self.assertIsNone(missing['price_acceleration_bps'])
        self.assertIsNone(missing['return_10s_bps'])


if __name__ == '__main__': unittest.main()
