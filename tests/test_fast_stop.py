import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from magi1.fast_observe import Observer, DAY
from magi1.fast_paper import Paper
from magi1.fast_experiment import Experiment
from magi2.fast_flow_paper_report import view

T=200*DAY


class FastStopTests(unittest.TestCase):
    def test_stop_cancels_entries_preserves_history_and_blocks_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'fast-observe'/'paper-v1.sqlite3'
            p=Paper(path,T)
            p.signal('KRW-X',T+1,100,95,{})
            before=p.s['cash']
            p.pause_entries(T+2)
            self.assertFalse(p.s['pending']);self.assertEqual(p.s['cash'],before)
            p.signal('KRW-Y',T+3,100,95,{})
            self.assertFalse(p.s['pending'])
            self.assertEqual(p.db.execute('SELECT count(*) FROM signals').fetchone()[0],1)
            self.assertIn('중지',view(root)[0])
            p.db.close();p=Paper(path,T+4)
            p.signal('KRW-Z',T+5,100,95,{})
            self.assertFalse(p.s['pending']);self.assertTrue(p.s['entries_paused'])
            p.db.close()

    def test_paused_account_still_exits_on_protection(self):
        with tempfile.TemporaryDirectory() as root:
            p=Paper(Path(root)/'paper.db',T)
            p.signal('KRW-X',T+1,100,95,{})
            def book(ts,bid,ask):
                return dict(bp=bid,ap=ask,stamp=ts,ts=ts,bids=[(bid,100000)],asks=[(ask,100000)])
            p.on_trade('KRW-X',T+2,100,1,T+2)
            p.on_book('KRW-X',T+3,book(T+3,99.99,100))
            held=copy.deepcopy(p.s['positions'])
            p.pause_entries(T+4)
            self.assertEqual(held,p.s['positions'])
            p.on_trade('KRW-X',T+5,94,1,T+5)
            p.on_book('KRW-X',T+6,book(T+6,94,94.1))
            self.assertFalse(p.s['positions'])
            self.assertEqual(p.db.execute("SELECT reason FROM fills WHERE side='SELL'").fetchone()[0],'PROTECTION')
            p.db.close()

    def test_capture_and_experiments_stop_but_shared_bollinger_windows_continue(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'observe.db';o=Observer(path,T)
            o.paper=Paper(Path(root)/'paper.db',T)
            o.experiment=Experiment(Path(root)/'experiments',T)
            bollinger=Mock();o.bollinger=bollinger
            o.disable_fast(T+1)
            o.connected('0',['KRW-X'],T+2)
            o.refresh_reference=Mock(side_effect=AssertionError('FAST evaluation must stop'))
            o.experiment.evaluate=Mock(side_effect=AssertionError('FAST experiment must stop'))
            o.evaluate(T+10000)
            bollinger.window.assert_called_once()
            self.assertTrue(o.report()['fast_disabled'])
            self.assertEqual(o.db.execute('SELECT count(*) FROM captures').fetchone()[0],0)
            for p in [o.paper,*o.experiment.accounts.values()]:
                self.assertTrue(p.s['entries_paused']);p.db.close()
            o.db.close();o=Observer(path,T+20000)
            self.assertTrue(o.fast_disabled);o.db.close()


if __name__=='__main__':unittest.main()
