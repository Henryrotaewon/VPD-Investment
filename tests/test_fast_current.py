import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from magi2.fast_price_monitor import price_signal, FastPriceMonitor, VERSION
from magi2.fast_tick_paper import CurrentTickLedger
from magi2.fast_reset import reset_once, RESET_ID
from magi2.fast_captures import captures
from magi2.fast_comparison import report
from magi2.fast_paper_report import recent
from test_fast_tick import START, RULES, tape, book, trade


class CurrentTests(unittest.TestCase):
    def test_five_percent_boundary_without_volume_or_acceleration(self):
        for p,want in [(104.999,False),(105,True),(106,True),(99,False)]:
            r=price_signal([(START,100),(START+300000,p)],START+300000)
            self.assertEqual(r['qualified'],want)
            self.assertIsNone(r['turnover_ratio'])
        self.assertIsNone(price_signal([(START,100),(START+360000,110)],START+360000))
        self.assertIsNone(price_signal([(START,float('nan')),(START+300000,110)],START+300000))
        r=price_signal([(START,100),(START+301000,105)],START+301000)
        self.assertEqual(r['actual_window_ms'],301000)

    def test_emitted_capture_and_comparison_have_new_rule_and_no_fake_volume(self):
        with tempfile.TemporaryDirectory() as root:
            paper=Mock();m=FastPriceMonitor(root,lambda _:None,paper)
            e=price_signal([(START,100),(START+300000,105)],START+300000)
            self.assertTrue(m.emit('upbit','KRW-T','T',e,104,105,START+300000,START+300000,1))
            self.assertEqual(paper.offer.call_args.kwargs['strategy_version'],VERSION)
            self.assertTrue(paper.offer.call_args.args[0].startswith('fast-v4:'))
            msg=m.events.get_nowait()['text']
            self.assertIn('+5%',msg);self.assertNotIn('거래대금',msg)
            text,_=captures(m.audit,START+300000)
            self.assertIn('v4',text);self.assertIn('300초',text)
            self.assertIn('v4',report(m.audit,START+300000))
            m.audit.db.close()

    def test_current_buy_then_current_plus_tick_sell_and_fixed_deadline(self):
        with tempfile.TemporaryDirectory() as root:
            l=CurrentTickLedger(Path(root)/'paper.db',START)
            l.offer('x','upbit','KRW-X',START,START,strategy_version=VERSION)
            l.limit_step('x',tape(START),None,RULES,START)
            t=l.get('x');self.assertEqual(t['order']['price'],100)
            self.assertEqual(t['execution_version'],'fast-current-cycle-v4')
            l.limit_step('x',tape(START+300),book(START+300,bid=99,ask=100),RULES,START+300)
            self.assertGreater(l.get('x')['remaining_qty'],0)
            self.assertIsNone(l.get('x')['order'])
            l.limit_step('x',tape(START+400,trade('a',START+350,100)),None,RULES,START+400)
            self.assertEqual(l.get('x')['order']['price'],101)
            self.assertEqual(l.get('x')['deadline_ms'],START+600000)
            self.assertIn('v4',recent(l,START+400))
            l.advance('upbit',START+600000)
            self.assertEqual(l.get('x')['status'],'EXIT_PENDING')
            l.exit('x',book(START+600300,bid=99,ask=100),START+600300)
            self.assertEqual(l.get('x')['status'],'CLOSED');l.close()

    def test_off_grid_current_price_rejected(self):
        l=CurrentTickLedger.__new__(CurrentTickLedger)
        with self.assertRaises(ValueError):l.buy_price(100.5,RULES)

    def test_reset_deletes_only_fast_and_never_repeats(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)
            for name in ['fast_paper.sqlite3','fast_paper.sqlite3-wal','fast_evidence.sqlite3','fast_evidence.sqlite3-shm','paper_state.json','magi3.sqlite3']:
                (p/name).write_text('sentinel')
            self.assertTrue(reset_once(p,lambda _:None))
            self.assertFalse((p/'fast_evidence.sqlite3').exists())
            self.assertEqual((p/'paper_state.json').read_text(),'sentinel')
            self.assertEqual((p/'magi3.sqlite3').read_text(),'sentinel')
            l=CurrentTickLedger(p/'fast_paper.sqlite3',START)
            self.assertEqual(l.account('upbit')['cash_quote'],1000000)
            self.assertEqual(l.account('bithumb')['cash_quote'],1000000)
            for v in ['binance','kraken']:
                l.fund(v,1400,'public',START)
                self.assertAlmostEqual(l.account(v)['cash_quote']*1400,1000000)
            l.offer('keep','upbit','KRW-X',START,START);l.close()
            self.assertFalse(reset_once(p,lambda _:None))
            l=CurrentTickLedger(p/'fast_paper.sqlite3',START+1)
            self.assertIsNotNone(l.get('keep'));self.assertEqual(l.started_ms,START);l.close()
            self.assertEqual(json.loads((p/'fast_reset.json').read_text())['reset_id'],RESET_ID)

if __name__=='__main__':unittest.main()
