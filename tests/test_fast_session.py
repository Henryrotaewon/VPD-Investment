import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from magi2.fast_session import day, bounds, KST
from magi2.fast_target_paper import TargetLedger
from magi2.fast_target_service import TargetPaperService
from magi2.fast_price_monitor import FastPriceMonitor
from magi2.fast_paper_report import positions_page, keyboard
from magi2.fast_captures import captures
from magi2.fast_reset import reset_once
from magi2.telegram_ui import main_keyboard, Confirmations
from magi3.fast_audit import FastAudit
from test_fast_tick import book, RULES


def ts(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=KST).timestamp()*1000)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.start=ts('2026-09-21T08:00:00')
        self.path=self.root/'paper.db';self.l=TargetLedger(self.path,self.start)
        for venue in ('binance','kraken'):self.l.fund(venue,1400,'test',self.start)

    def tearDown(self):
        self.l.close();self.tmp.cleanup()

    def buy(self,ident='x',when=None):
        when=self.start if when is None else when
        self.assertTrue(self.l.offer(ident,'upbit','KRW-'+ident,when,when))
        self.assertTrue(self.l.enter_market(ident,book(when+300,99,100,100000),RULES,when+300))

    def test_reset_exact_files_once_and_four_million_seed(self):
        for name in ('fast_paper.sqlite3','fast_paper.sqlite3-wal','fast_evidence.sqlite3','fast_evidence.sqlite3-shm','paper_state.json','unrelated.txt'):
            (self.root/name).write_text('old')
        self.assertTrue(reset_once(self.root,lambda _:None))
        self.assertFalse((self.root/'fast_evidence.sqlite3').exists())
        self.assertEqual((self.root/'paper_state.json').read_text(),'old')
        (self.root/'fast_paper.sqlite3').write_text('new')
        self.assertFalse(reset_once(self.root,lambda _:None))
        self.assertEqual((self.root/'fast_paper.sqlite3').read_text(),'new')
        text,_=positions_page(self.l,self.start)
        self.assertIn('최초원금 4,000,000원',text)
        self.assertIn('예수금 4,000,000원',text)
        self.assertIn('누적 +0.00%',text)

    def test_pause_cancels_entries_blocks_late_book_and_survives_restart(self):
        self.buy()
        self.l.offer('pending','upbit','KRW-WAIT',self.start+500,self.start+500)
        result=self.l.pause_and_clear(self.start+600)
        self.assertEqual(result,dict(positions=1,canceled_entries=1))
        self.assertFalse(self.l.enter_market('pending',book(self.start+800,99,100,100000),RULES,self.start+800))
        self.assertFalse(self.l.offer('late','upbit','KRW-LATE',self.start+800,self.start+800))
        self.l.close();self.l=TargetLedger(self.path,self.start+900)
        self.assertFalse(self.l.control()['enabled'])
        self.assertFalse(self.l.resume(self.start+1000))
        self.l.exit('x',book(self.start+1100,99,100,100000),self.start+1100)
        self.assertFalse(self.l.control()['enabled'])
        self.assertTrue(self.l.resume(self.start+1200))
        self.assertFalse(self.l.offer('old','upbit','KRW-OLD',self.start+1199,self.start+1200))
        self.assertTrue(self.l.offer('new','upbit','KRW-NEW',self.start+1201,self.start+1201))

    def test_0730_reentry_boundary_preserves_capital_and_daily_report(self):
        self.buy()
        self.l.liquidate_all(self.start+1000)
        self.l.exit('x',book(self.start+1100,99,100,100000),self.start+1100)
        before=ts('2026-09-22T07:29:59');after=before+1000
        cash=self.l.account('upbit')['cash_quote']
        self.assertFalse(self.l.offer('blocked','upbit','KRW-x',before,before))
        self.assertTrue(self.l.offer('allowed','upbit','KRW-x',after,after))
        self.assertEqual(self.l.account('upbit')['cash_quote'],cash)
        self.assertEqual(day(before),'2026-09-21');self.assertEqual(day(after),'2026-09-22')
        self.assertIsNone(self.l.due_day(before));self.assertEqual(self.l.due_day(after),'2026-09-21')
        snap=self.l.snapshot(after,'2026-09-21')
        self.assertEqual(snap['accounts'][0]['closed'],1)

    def test_0730_does_not_reset_holdings_cash_or_cumulative_return(self):
        before=ts('2026-09-22T07:29:59');after=before+1000
        self.buy(when=before-500)
        self.l.mark('upbit',{'KRW-x':(105,106)},before)
        header_before,_=positions_page(self.l,before)
        cash=self.l.account('upbit')['cash_quote'];cost=self.l.get('x')['remaining_cost']
        header_after,_=positions_page(self.l,after)
        self.assertEqual(self.l.account('upbit')['cash_quote'],cash)
        self.assertEqual(self.l.get('x')['remaining_cost'],cost)
        self.assertEqual(self.l.get('x')['status'],'OPEN')
        self.assertEqual(next(x for x in header_before.splitlines() if x.startswith('평가 ')),
                         next(x for x in header_after.splitlines() if x.startswith('평가 ')))
        self.assertIn('최초원금 4,000,000원',header_after)

    def test_available_cash_reserved_five_slots_and_cap_after_profit(self):
        with self.l.lock,self.l.db:
            a=self.l._account('upbit');a['cash_quote']=850000;self.l._save_account(a)
        for i in range(5):self.assertTrue(self.l.offer(str(i),'upbit','KRW-'+str(i),self.start,self.start))
        self.assertEqual([self.l.get(str(i))['budget_quote'] for i in range(5)],[200000]*4+[50000])
        self.assertFalse(self.l.offer('six','upbit','KRW-SIX',self.start,self.start))
        for i in range(5):self.l.enter_market(str(i),book(self.start+300,99,100,100000),RULES,self.start+300)
        self.assertGreaterEqual(self.l.account('upbit')['cash_quote'],0)
        self.assertLess(self.l.account('upbit')['cash_quote'],1)
        self.assertLessEqual(max(t['entry_cost'] for t in self.l.active('upbit')),200000)

    def test_capture_window_latest_symbol_only_simple(self):
        audit=FastAudit(self.root/'a.db');boundary=bounds(day(self.start))[0]
        for ident,stamp,asset in [('old',boundary-1,'OLD'),('first',boundary,'A'),('new',boundary+2,'A'),('b',boundary+1,'B')]:
            audit.record('SIGNAL_DETECTED',ident,'upbit','KRW-'+asset,'ALERT_ONLY',ts_ms=stamp,
                         rule_version='fast-price-rise-v4',observed={'asset':asset})
        text,_=captures(audit,self.start)
        self.assertNotIn('OLD',text);self.assertEqual(text.count('업비트 · A'),1)
        self.assertLess(text.index('업비트 · A'),text.index('업비트 · B'))
        self.assertNotIn('강도',text);self.assertNotIn('수익률',text)
        audit.db.close()

    def test_main_and_submenu_exact_and_start_requires_confirm(self):
        from magi2 import server_runner as server
        main=[b['text'] for r in main_keyboard()['keyboard'] for b in r]
        self.assertIn('지표가속 모의투자',main)
        self.assertNotIn('FAST 모의투자',main)
        self.assertEqual([b['text'] for r in keyboard()['inline_keyboard'] for b in r],
            ['포착 리스트','모의투자 결과','일괄정리 및 포착정지','포착 및 매매 시작','일별 평가','관측 상태','매매 이력','전략 설명'])
        paper=Mock();paper.resume.return_value=True
        paper.ledger.policy_review_required=False  # Legacy confirmation path; held mode has its own test.
        confirmations=Confirmations();callback=lambda data,user='7':{'id':'x','data':data,'from':{'id':user},'message':{'message_id':1,'chat':{'id':'7'}}}
        with patch.object(server,'FAST_PAPER',paper),patch.object(server,'CONFIRMATIONS',confirmations),patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'ALLOWED_USER_IDS',{'7','8'}),patch.object(server,'telegram') as send,patch.object(server,'telegram_api'):
            server.handle_callback(callback('nav:fast_start'));paper.resume.assert_not_called()
            buttons=send.call_args.args[1]['inline_keyboard'][0]
            server.handle_callback(callback(buttons[0]['callback_data'],'8'));paper.resume.assert_not_called()
            server.handle_callback(callback(buttons[1]['callback_data']));paper.resume.assert_not_called()
            server.handle_callback(callback('nav:fast_start'))
            yes=send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
            server.handle_callback(callback(yes));server.handle_callback(callback(yes));paper.resume.assert_called_once()

    def test_inflight_capture_is_rejected_on_stop_and_after_resume(self):
        paper=Mock();paper.ledger=self.l
        monitor=FastPriceMonitor(self.root,lambda _:None,paper)
        evidence={'window_end_ms':self.start,'return_5m_bps':600}
        self.l.pause_and_clear(self.start+100)
        self.assertFalse(monitor.emit('upbit','KRW-X','X',evidence,99,100,self.start+200,self.start,1))
        self.l.resume(self.start+300)
        self.assertFalse(monitor.emit('upbit','KRW-X','X',evidence,99,100,self.start+400,self.start,1))
        self.assertEqual(monitor.audit.db.execute("SELECT COUNT(*) FROM fast_events WHERE event_type='SIGNAL_DETECTED'").fetchone()[0],0)
        monitor.audit.db.close()


if __name__=='__main__':unittest.main()
