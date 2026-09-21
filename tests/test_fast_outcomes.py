import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from magi2.fast_target_paper import TargetLedger
from magi2.fast_target_service import TargetPaperService
from magi2.fast_paper_report import positions_page, money
from magi2.fast_session import day, bounds
from test_fast_tick import START, RULES, book


class OutcomesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'paper.db'
        self.l=TargetLedger(self.path,START)
        for v in ('binance','kraken'):self.l.fund(v,1400,'test',START)

    def tearDown(self):
        self.l.close();self.tmp.cleanup()

    def service(self, when, logs):
        s=TargetPaperService.__new__(TargetPaperService)
        s.ledger=self.l;s.clock=lambda:when;s.log=logs.append
        return s

    def test_immediate_stop_cash_loss_is_visible_and_survives_restart(self):
        self.l.offer('sn8','kraken','SN8USD',START,START)
        logs=[];market=Mock();market.rules.return_value=RULES
        market.book.return_value=book(START+300,99,100,100000)
        self.service(START+300,logs).step('kraken',market)
        market.book.return_value=book(START+1300,55,56,100000)
        self.service(START+1300,logs).step('kraken',market)
        self.service(START+2300,logs).step('kraken',market)
        t=self.l.get('sn8');fx=self.l.account('kraken')['fx_krw_per_quote']
        self.assertEqual(t['status'],'CLOSED')
        states=[json.loads(line.split(' ',1)[1]) for line in logs if line.startswith('fast_paper_state ')]
        self.assertEqual([x['status'] for x in states],['OPEN','CLOSED'])
        self.assertEqual(states[0]['entry_bid'],99)
        cash=sum(self.l.account(v)['cash_quote']*self.l.account(v)['fx_krw_per_quote'] for v in ('upbit','bithumb','binance','kraken'))
        self.assertAlmostEqual(cash,4000000+t['realized_quote']*fx,places=6)
        before=self.l.db.total_changes
        report,_=positions_page(self.l,START+2300)
        self.assertIn('보유·매수대기 0건',report);self.assertIn('SN8USD',report)
        self.assertIn('매도 완료',report);self.assertIn('−6% 손절',report)
        self.assertIn('확정 순손익 '+money(t['realized_quote']*fx,True),report)
        self.assertIn('누적 실현손익 '+money(t['realized_quote']*fx,True)+' / 보유 평가손익 +0원',report)
        self.assertIn(f'({t["realized_quote"]/t["entry_cost"]*100:+.2f}%)',report)
        self.assertIn('매도수령액 '+money(t['exit_proceeds']*fx),report)
        self.assertEqual(self.l.db.total_changes,before)
        self.l.close();self.l=TargetLedger(self.path,START+3000)
        restored=[];self.service(START+3000,restored).log_restored_state()
        self.assertTrue(any('"phase":"restored"' in line and 'SN8USD' in line for line in restored))
        self.assertEqual(self.l.get('sn8'),t)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],2)
        self.assertIn('SN8USD',positions_page(self.l,START+3000)[0])

    def test_expired_entry_keeps_reason_without_fake_fill_or_loss(self):
        self.l.offer('wait','upbit','KRW-WAIT',START,START)
        self.l.fail('wait','INSUFFICIENT_ASK_DEPTH')
        logs=[];market=Mock()
        self.service(START+10001,logs).step('upbit',market)
        self.service(START+11001,logs).step('upbit',market)
        market.book.assert_not_called()
        self.assertEqual(len([x for x in logs if 'entry_expired' in x]),1)
        report,_=positions_page(self.l,START+11001)
        self.assertIn('KRW-WAIT',report);self.assertIn('미매수 · 10초 내',report)
        self.assertIn('INSUFFICIENT_ASK_DEPTH',report)
        self.assertNotIn('확정 순손익',report)
        self.assertIn('예수금 4,000,000원',report)
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],0)

    def test_recent_outcomes_boundary_limit_and_telegram_size(self):
        boundary=bounds(day(START))[1]
        # A result belongs to the day it completes, even if captured the prior day.
        for i in range(12):
            ident=f'x{i}'
            self.l.offer(ident,'upbit','KRW-'+ident,START,START)
            with self.l.lock,self.l.db:
                t=self.l.get(ident)
                t.update(status='CLOSED',close_ms=boundary+i,entry_ms=START+300,
                         entry_cost=200000.,entry_qty=2000.,buy_average=100.,
                         exit_proceeds=111051.,realized_quote=-88949.,reason='STOP_LOSS_6')
                self.l._save(t)
        self.assertEqual(self.l.recent_outcomes(boundary-1),[])
        self.assertEqual([t['id'] for t in self.l.recent_outcomes(boundary)],['x0'])
        self.assertEqual(len(self.l.recent_outcomes(boundary+11)),10)
        report,_=positions_page(self.l,boundary+11)
        self.assertLess(len(report),4096)


if __name__=='__main__':unittest.main()
