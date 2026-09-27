import json
import tempfile
import unittest
from pathlib import Path
from magi1.fast_experiment import Experiment
from magi1.fast_paper import Paper
from magi1.fast_observe import Observer, DAY, FIVE


class ExperimentTests(unittest.TestCase):
    def test_first_window_then_control_and_isolated_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            start = 30*DAY
            o = Observer(str(Path(directory)/'observe.db'), start)
            o.paper = Paper(str(Path(directory)/'main.db'), start)
            o.experiment = Experiment(str(Path(directory)/'experiment'), start)
            o.connected('a', ['KRW-X'], start)
            for k in range(1,11):
                o.db.execute('INSERT INTO baseline VALUES(?,?,?)', ('KRW-X',start-k*DAY,100))
            o.db.execute('INSERT INTO recent5m VALUES(?,?,100,100,99,100,3,300,?)', ('KRW-X',start,'API'))
            o.current = start+FIVE+60000
            for i in range(6):
                o.bars['KRW-X'].append(dict(t=o.current-60000+i*10000,value=1,buy=1,count=1,ofi=1,l=99))
            def window(value):
                o.bar('KRW-X').update(value=value,buy=value,count=20,ofi=1,l=99)
                end=o.current+10000
                o.books['KRW-X']=dict(ts=end-1,stamp=end-1,bp=100,ap=100.1,bq=10000,aq=10000)
                o.last_price['KRW-X']=(end-1,100,end-1)
                o.advance(end)
                return end
            first=window(30)
            early=o.experiment.accounts['early']; control=o.experiment.accounts['control']
            self.assertIn('KRW-X',early.s['pending'])
            self.assertFalse(control.s['pending']); self.assertFalse(o.paper.s['pending'])
            window(90)
            self.assertIn('KRW-X',control.s['pending']); self.assertIn('KRW-X',o.paper.s['pending'])
            evidence=json.loads(control.db.execute('SELECT payload FROM signals').fetchone()[0])
            self.assertEqual(evidence['first_condition_ms'],first)
            self.assertEqual(evidence['confirmation_delay_ms'],10000)
            # A subsequent public book/trade fills all eligible accounts independently.
            ts=o.current+1
            o.ingest(dict(type='trade',code='KRW-X',trade_timestamp=ts,sequential_id=1,
                          trade_price=100,trade_volume=1,ask_bid='BID'),ts)
            o.ingest(dict(type='orderbook',code='KRW-X',timestamp=ts,
                          orderbook_units=[dict(bid_price=100,ask_price=100.1,bid_size=10000,ask_size=10000)]),ts)
            self.assertIn('KRW-X',control.s['positions'])
            self.assertIn('KRW-X',o.paper.s['positions'])
            self.assertFalse(early.s['positions']) # its 10-second wait expired, no retrospective fill
            self.assertEqual(early.s['cash'],3000000)
            o.gap('a',ts+1,'TEST')
            self.assertTrue(control.s['positions']['KRW-X']['uncertain'])
            for p in o.papers:p.close(ts+1)
            o.db.close()

    def test_no_bypass_of_missing_history_and_policy_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            o=Observer(str(Path(directory)/'obs.db'),30*DAY)
            o.experiment=Experiment(str(Path(directory)/'exp'),30*DAY)
            o.connected('a',['KRW-X'],30*DAY)
            for i in range(1,10):o.advance(30*DAY+i*10000)
            self.assertTrue(all(not p.s['pending'] for p in o.experiment.accounts.values()))
            for p in o.papers:p.close(o.current)
            with self.assertRaises(ValueError):Paper(str(Path(directory)/'exp/early.sqlite3'),o.current)
            o.db.close()

    def test_retry_requires_reset_and_restart_preserves_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            e=Experiment(directory,30*DAY); end=30*DAY+100000
            bars=[dict(t=end-60000+i*10000,l=99) for i in range(6)]
            f=dict(relative_value=3,net_buy=100)
            row=('KRW-X',f,100,True,None,bars)
            e.evaluate(end,end,[row]); early=e.accounts['early']
            early.cancel('KRW-X',end+1,'TEST')
            e.evaluate(end+10000,end+10000,[('KRW-X',f,100,True,(end,True),bars)])
            self.assertFalse(early.s['pending'])
            e.evaluate(end+20000,end+20000,[('KRW-X',f,100,False,None,bars)])
            later=end+30000
            bars=[dict(t=later-60000+i*10000,l=99) for i in range(6)]
            e.evaluate(later,later,[('KRW-X',f,100,True,None,bars)])
            self.assertIn('KRW-X',early.s['pending'])
            for p in e.accounts.values():p.close(later)
            e=Experiment(directory,later+1)
            self.assertEqual(e.accounts['early'].s['started_ms'],30*DAY)
            self.assertFalse(e.accounts['early'].s['pending'])
            for p in e.accounts.values():p.close(later+1)
