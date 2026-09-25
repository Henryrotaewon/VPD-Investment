import json
from pathlib import Path
import tempfile
import unittest
from magi2.hourly_indicator import HourlyLedger,recovery_point,DAY,HOUR,STEP,COHORT
from magi2.hourly_indicator_report import positions,menu,daily_view,events_view

T=200*DAY+HOUR

def point(t,qualifies=True,close=100,trend=False):
    return dict(boundary=t,day=(t-1)//DAY*DAY,qualifies=qualifies,close=close,low=95,trend_exit=trend)


class RecoveryTests(unittest.TestCase):
    def test_archived_ark_matches_research_and_excludes_future(self):
        f=json.loads(Path('tests/fixtures/hourly_ark_recovery.json').read_text())
        for expected in f['expected']:
            b=expected['observed_ms'];p=recovery_point(f['daily'],f['hours'],b)
            self.assertTrue(p['qualifies'])
            for k in ('values','velocity'):
                self.assertEqual(p[k],expected[k])
            self.assertEqual(p['acceleration'],expected['acceleration'])
            future=[b,999,999,999,999,999]
            self.assertEqual(p,recovery_point(f['daily']+[future],f['hours']+[future],b))
    def test_gap_and_warmup_rejected(self):
        f=json.loads(Path('tests/fixtures/hourly_ark_recovery.json').read_text());b=f['expected'][-1]['observed_ms']
        with self.assertRaisesRegex(ValueError,'WARMUP'):recovery_point(f['daily'][-99:],f['hours'],b)
        with self.assertRaisesRegex(ValueError,'GAP'):recovery_point(f['daily'][:-1],f['hours'],b)
        with self.assertRaisesRegex(ValueError,'NO_TRADE'):recovery_point(f['daily'],f['hours'][:-1],b)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.ledger=HourlyLedger(self.tmp.name,T-1)
    def tearDown(self):self.ledger.db.close();self.tmp.cleanup()
    def candidate(self,symbol,t=T):
        self.ledger.observe(symbol,point(t),t+1000,1)
        self.ledger.observe(symbol,point(t+HOUR),t+HOUR+1000,1)
    def fill(self,symbol,price=100):
        o=self.ledger.pending()[symbol];t=o['earliest']
        self.assertTrue(self.ledger.execute(symbol,o,[t,price,price,price,price,1],t+1))
        return t
    def test_no_past_or_cross_day_confirmation(self):
        l=self.ledger
        l.observe('A',point(T-HOUR),T,1);l.observe('A',point(T),T+1000,1)
        self.assertFalse(l.pending())
        l.observe('B',point(201*DAY),201*DAY+1000,1)
        l.observe('B',point(201*DAY+HOUR),201*DAY+HOUR+1000,1)
        self.assertFalse(l.pending())
    def test_ten_equal_slots_no_overcommit_and_restart(self):
        for i in range(12):self.candidate('KRW-'+str(i))
        self.assertEqual(len(self.ledger.pending()),10)
        self.assertEqual({o['budget'] for o in self.ledger.pending().values()},{300000})
        for s in list(self.ledger.pending()):self.fill(s)
        self.assertAlmostEqual(self.ledger.s['cash'],0)
        self.assertEqual(len(self.ledger.s['positions']),10)
        self.ledger.db.close();self.ledger=HourlyLedger(self.tmp.name,T+3*HOUR)
        self.assertEqual(len(self.ledger.s['positions']),10)
        self.assertAlmostEqual(self.ledger.s['cash'],0)
    def test_future_fill_only_costs_exit_and_no_reuse(self):
        l=self.ledger;self.candidate('A');o=l.pending()['A']
        self.assertFalse(l.execute('A',o,[o['earliest']-STEP,100],o['earliest']+1))
        entry=self.fill('A')
        self.assertAlmostEqual(l.s['positions']['A']['qty'],300000/(100*1.0005**2))
        l.observe('A',point(T+2*HOUR,close=90),T+2*HOUR+1000,1)
        self.assertEqual(l.pending()['A']['side'],'SELL')
        self.fill('A',85)
        self.assertAlmostEqual(l.s['cash'],2700000+300000*85/100*.9995**2/1.0005**2)
        l.observe('A',point(T+3*HOUR),T+3*HOUR+1000,1)
        self.assertFalse(l.pending())
        self.assertGreater(l.s['last_exit']['A'],entry)
    def test_clear_cancels_pending_race_and_resume_needs_new_evidence(self):
        l=self.ledger;self.candidate('A');order=l.pending()['A']
        result=l.clear(T+HOUR+2000);self.assertEqual(result['canceled_entries'],1)
        self.assertFalse(l.execute('A',order,[order['earliest'],100],order['earliest']+1))
        self.assertTrue(l.resume(T+HOUR+3000));self.assertFalse(l.s['previous'])
    def test_daily_exit_requires_both_conditions(self):
        self.candidate('A');self.fill('A')
        self.ledger.observe('A',point(T+2*HOUR,False,99,False),T+2*HOUR+1000,1)
        self.assertFalse(self.ledger.pending())
        self.ledger.observe('A',point(T+3*HOUR,False,99,True),T+3*HOUR+1000,1)
        self.assertEqual(self.ledger.pending()['A']['side'],'SELL')
    def test_reset_scope_and_ui(self):
        root=Path(self.tmp.name);vpd=root/'paper_state.json';vpd.write_text('untouched')
        old=root/'indicator_paper'/'indicator-paper-old';old.mkdir();(old/'db').write_text('old')
        # First activation removes old cohorts; restarts do not wipe the new one.
        with self.ledger.lock:self.ledger.s['reset_done']=False;self.ledger.save()
        self.ledger.db.close();self.ledger=HourlyLedger(root,T)
        self.assertFalse(old.exists());self.assertEqual(vpd.read_text(),'untouched')
        self.ledger.mark({},T)
        for text,markup in (positions(self.ledger,T),menu(self.ledger),daily_view(self.ledger,'1970-07-20',T),events_view(self.ledger,T)):
            self.assertLess(len(text),4096);self.assertTrue(markup['inline_keyboard'])
        self.assertIn('3,000,000',positions(self.ledger,T)[0])

if __name__=='__main__':unittest.main()
