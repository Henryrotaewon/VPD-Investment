import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from magi2.fast_target_paper import TargetLedger, target_price
from magi2.fast_target_service import TargetPaperService
from magi2.fast_tick_rules import UPBIT_GRID
from magi2.fast_paper import bounds, day
from magi2.fast_paper_report import recent, history, summary, keyboard
from magi2.telegram_ui import parse_command
from test_fast_tick import START, RULES, book, tape, trade


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'p.db'
        self.l=TargetLedger(self.path,START)
    def tearDown(self):
        self.l.close();self.tmp.cleanup()
    def enter(self,ident='x',symbol='KRW-X',venue='upbit',ts=START):
        self.assertTrue(self.l.offer(ident,venue,symbol,ts,ts))
        self.assertTrue(self.l.enter_market(ident,book(ts+300,99,100,100000),RULES,ts+300))
        return self.l.get(ident)
    def test_market_entry_fixed_target_no_ten_minute_exit(self):
        t=self.enter()
        self.assertAlmostEqual(t['buy_average'],100.05)
        self.assertEqual(t['take_profit_price'],113)
        self.assertAlmostEqual(t['stop_price'],94.047)
        self.assertEqual(t['order']['price'],113)
        self.assertIsNone(t['deadline_ms'])
        self.l.advance('upbit',START+86400000)
        self.assertEqual(self.l.get('x')['status'],'OPEN')
        self.assertEqual(self.l.get('x')['order']['price'],113)
        self.assertLessEqual(t['entry_cost'],200000)
    def test_target_rounding_crosses_band(self):
        self.assertEqual(target_price(100,RULES),112)
        self.assertEqual(target_price(89.3,dict(grid=UPBIT_GRID)),101)
    def test_take_profit_no_rebuy_and_persistent_day_block(self):
        self.enter()
        self.l.limit_step('x',tape(START+700),book(START+700,113,114,100000),RULES,START+700)
        t=self.l.get('x');self.assertEqual(t['status'],'CLOSED')
        self.assertEqual(t['reason'],'TAKE_PROFIT_12');self.assertGreater(t['realized_quote'],0)
        self.assertFalse(self.l.limit_step('x',tape(START+900),None,RULES,START+900))
        self.l.close();self.l=TargetLedger(self.path,START+1000)
        self.assertFalse(self.l.offer('again','upbit','KRW-X',START+1000,START+1000))
        self.assertEqual(self.l.get('again')['reason'],'SOLD_TODAY_KST')
        tomorrow=bounds(day(START))[1]
        self.assertTrue(self.l.offer('tomorrow','upbit','KRW-X',tomorrow,tomorrow))
    def test_stop_boundary_cancels_limit_and_market_exits(self):
        t=self.enter();stop=t['stop_price']
        self.assertFalse(self.l.check_stop('x',book(START+900,stop+.001,100),START+900))
        b=book(START+1000,stop,100,100000)
        self.assertTrue(self.l.check_stop('x',b,START+1000))
        self.assertIsNone(self.l.get('x')['order'])
        self.assertTrue(self.l.exit('x',b,START+1000))
        t=self.l.get('x');self.assertEqual(t['status'],'CLOSED');self.assertEqual(t['reason'],'STOP_LOSS_6')
        self.assertLess(t['realized_quote']/t['entry_cost'],-.06)
        self.assertAlmostEqual(t['gross_pnl']-t['fees_paid']-t['slippage_paid'],t['realized_quote'],places=6)
        self.assertFalse(self.l.offer('again','upbit','KRW-X',START+1100,START+1100))
    def test_clear_all_venues_and_pending_idempotent(self):
        for v in ['upbit','bithumb','binance','kraken']:
            self.l.fund(v,1400,'test',START)
            self.enter(v,'X',v)
        self.l.offer('waiting','upbit','KRW-Y',START,START)
        r=self.l.liquidate_all(START+1000)
        self.assertEqual(r,dict(positions=4,canceled_entries=1))
        self.assertEqual(self.l.get('waiting')['status'],'SKIPPED')
        for v in ['upbit','bithumb','binance','kraken']:
            self.assertIsNone(self.l.get(v)['order'])
            self.assertEqual(self.l.get(v)['reason'],'MANUAL_FAST_CLEAR')
        self.l.close();self.l=TargetLedger(self.path,START+1100)
        self.l.liquidate_all(START+1100)
        for v in ['upbit','bithumb','binance','kraken']:
            self.l.exit(v,book(START+1200,99,100,100000),START+1200)
            self.assertEqual(self.l.get(v)['remaining_qty'],0)
        self.assertEqual(self.l.liquidate_all(START+1300)['positions'],0)
    def test_partial_exit_persists_and_never_rebuys(self):
        self.enter();self.l.liquidate_all(START+1000)
        self.l.exit('x',book(START+1100,99,100,1),START+1100)
        self.assertEqual(self.l.get('x')['status'],'EXIT_PENDING')
        self.l.close();self.l=TargetLedger(self.path,START+1200)
        self.l.exit('x',book(START+1300,98,99,100000),START+1300)
        self.assertEqual(self.l.get('x')['status'],'CLOSED')
    def test_stale_or_insufficient_book_cannot_invent_entry(self):
        self.l.offer('x','upbit','KRW-X',START,START)
        with self.assertRaises(ValueError):self.l.enter_market('x',book(START,99,100,100000),RULES,START+300)
        with self.assertRaises(ValueError):self.l.enter_market('x',book(START+300),RULES,START+300)
        self.assertEqual(self.l.get('x')['remaining_qty'],0)
        self.l.advance('upbit',START+10001)
        self.assertEqual(self.l.get('x')['reason'],'ENTRY_QUOTE_TIMEOUT')
    def test_stop_service_does_not_depend_on_tape(self):
        self.enter()
        service=TargetPaperService.__new__(TargetPaperService)
        service.ledger=self.l;service.clock=lambda:START+1000;service.log=lambda _:None
        market=Mock();market.book.return_value=book(START+1000,90,91,100000)
        market.tape.side_effect=RuntimeError('unavailable')
        service.step('upbit',market)
        self.assertEqual(self.l.get('x')['status'],'CLOSED')
        market.tape.assert_not_called()
    def test_clear_command_and_callback_enforce_actor_permission(self):
        from magi2 import server_runner as server
        paper=Mock();paper.clear.return_value=dict(positions=2,canceled_entries=1)
        with patch.object(server,'FAST_PAPER',paper), patch.object(server,'telegram') as send, patch.object(server,'telegram_api'), patch.object(server,'ALLOWED_CHAT_ID','7'), patch.object(server,'may_execute',return_value=False) as allowed:
            server.handle_command('/fast_clear','7','8')
            paper.clear.assert_not_called()
            allowed.return_value=True
            server.handle_callback({'id':'c','data':'nav:fast_clear','from':{'id':'7'},'message':{'chat':{'id':'7'}}})
            paper.clear.assert_called_once()
            self.assertIn('정리 접수',send.call_args.args[0])

    def test_report_and_button(self):
        self.enter()
        self.assertIn('v5',recent(self.l,START+1000))
        self.assertIn('손절 기준',history(self.l,START+1000))
        msg=summary(self.l.snapshot(START+1000))
        self.assertIn('시간제한 없음',msg);self.assertLess(len(msg),3500)
        self.assertIn('nav:fast_clear',str(keyboard()))
        self.assertEqual(parse_command('FAST 정리'),'fast_clear')
        self.assertEqual(parse_command('/fast_clear'),'fast_clear')

if __name__=='__main__':unittest.main()
