import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from magi2.hourly_indicator import HourlyLedger, HOUR, STEP, DAY
from magi2.indicator_rebalance import candidates, plan, request, advance, cancel_entry, preview_text, RebalanceUnavailable
from magi2.hourly_indicator_report import positions
from magi2.telegram_ui import Confirmations
from magi2 import server_runner as server

T=200*DAY+2*HOUR


def point(t):
    return dict(boundary=t,day=(t-1)//DAY*DAY,qualifies=True,close=100,low=95,trend_exit=False)


class PortfolioActions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.l=HourlyLedger(self.tmp.name,T-HOUR-1)
        # Actual observe() path: ten automatic entries, two slot-skipped candidates.
        for i in range(12):
            symbol=f'KRW-A{i:02}'
            self.l.observe(symbol,point(T-HOUR),T-HOUR+1000,1)
            self.l.observe(symbol,point(T),T+1000,1)
        for symbol in list(self.l.pending()):self.fill(symbol)
        self.l.s['scan']=dict(status='MONITORING',boundary=T,bootstrap=False,
                             names={f'KRW-A{i:02}':f'종목{i}' for i in range(12)})
        self.l.save()
        self.stamp=T+2*STEP
    def tearDown(self):
        self.l.db.close();self.tmp.cleanup()
    def fill(self,symbol,price=100):
        order=self.l.pending()[symbol]
        self.assertTrue(self.l.execute(symbol,order,[order['earliest'],price],order['earliest']+1))
    def preview(self,mode):return plan(self.l,mode,self.stamp)
    def run_action(self,mode):
        prepared=self.preview(mode)
        return request(self.l,mode,self.stamp,prepared['fingerprint'])
    def test_pool_counts_unique_current_confirmations_not_history(self):
        self.assertEqual(len(candidates(self.l,self.stamp)),12)
        self.l.s['confirmed']['KRW-A00']['qualifies']=False
        self.l.s['confirmed']['KRW-A01']['boundary']-=HOUR
        self.l.s['confirmed']['KRW-A02']['day']-=DAY
        before=copy.deepcopy(self.l.s)
        prepared=self.preview('rebuild')
        self.assertEqual(prepared['candidates'],9)
        self.assertEqual(len(prepared['targets']),9)
        self.assertTrue(set(prepared['targets']).isdisjoint({'KRW-A00','KRW-A01','KRW-A02'}))
        self.assertEqual(before,self.l.s)
        self.assertEqual(candidates(self.l,T+HOUR),{})
        self.l.s['scan']['status']='SCANNING'
        self.assertEqual(candidates(self.l,self.stamp),{})
    def test_six_candidate_rebuild_keeps_four_slot_budgets_in_cash(self):
        self.l.s['confirmed']={k:v for k,v in self.l.s['confirmed'].items() if k<'KRW-A06'}
        prepared=self.preview('rebuild')
        self.assertIn('나머지 4슬롯 몫은 현금 유지',preview_text(prepared))
        self.run_action('rebuild')
        self.assertEqual(len(self.l.pending()),10)
        self.assertTrue(all(o['side']=='SELL' for o in self.l.pending().values()))
        for symbol in list(self.l.pending()):self.fill(symbol)
        settled=self.l.s['cash']
        advance(self.l,self.stamp+STEP+100)
        self.assertEqual(len(self.l.pending()),6)
        for order in self.l.pending().values():self.assertAlmostEqual(order['budget'],settled/10)
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,self.stamp+2*STEP+100)
        self.assertEqual(len(self.l.s['positions']),6)
        self.assertAlmostEqual(self.l.s['cash'],settled*0.4)
        self.assertEqual(self.l.s['rebalance']['phase'],'DONE')
        self.assertEqual(self.l.s['rebalance']['reason'],'PARTIAL_CASH_RETAINED')
    def test_six_candidate_refill_excludes_holdings_and_keeps_unfilled_budget(self):
        for symbol in ('KRW-A00','KRW-A01','KRW-A02'):
            self.l.schedule_exit(symbol,T+STEP+1,'TEST',T)
            self.fill(symbol)
        selected={'KRW-A03','KRW-A04','KRW-A05','KRW-A06','KRW-A10','KRW-A11'}
        self.l.s['confirmed']={k:v for k,v in self.l.s['confirmed'].items() if k in selected}
        kept=copy.deepcopy(self.l.s['positions']);cash=self.l.s['cash']
        prepared=self.preview('refill')
        self.assertEqual(prepared['candidates'],6)
        self.assertEqual(prepared['vacant'],3)
        self.assertIn('나머지 1슬롯 몫은 현금 유지',preview_text(prepared))
        self.run_action('refill')
        self.assertEqual(set(self.l.pending()),{'KRW-A10','KRW-A11'})
        self.assertEqual(self.l.s['positions'],kept)
        for order in self.l.pending().values():self.assertAlmostEqual(order['budget'],cash/3)
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,self.stamp+STEP+100)
        self.assertAlmostEqual(self.l.s['cash'],cash/3)
        for symbol,p in kept.items():self.assertEqual(self.l.s['positions'][symbol],p)
    def test_single_candidate_can_refill_without_concentrating_cash(self):
        for symbol in ('KRW-A00','KRW-A01'):
            self.l.schedule_exit(symbol,T+STEP+1,'TEST',T)
            self.fill(symbol)
        self.l.s['confirmed']={'KRW-A10':self.l.s['confirmed']['KRW-A10']}
        cash=self.l.s['cash']
        self.run_action('refill')
        self.assertEqual(list(self.l.pending()),['KRW-A10'])
        self.assertAlmostEqual(self.l.pending()['KRW-A10']['budget'],cash/2)
    def test_zero_candidates_never_liquidates_or_creates_a_request(self):
        self.l.schedule_exit('KRW-A00',T+STEP+1,'TEST',T)
        self.fill('KRW-A00')
        self.l.s['confirmed']={}
        before=copy.deepcopy(self.l.s)
        for mode in ('refill','rebuild'):
            with self.subTest(mode=mode),self.assertRaisesRegex(RebalanceUnavailable,'유효 포착 0종목'):
                request(self.l,mode,self.stamp,'unused')
        self.assertEqual(self.l.s,before)
        self.assertFalse(self.l.pending())
    def test_all_candidates_held_and_scan_unavailable_have_distinct_reasons(self):
        self.l.schedule_exit('KRW-A00',T+STEP+1,'TEST',T)
        self.fill('KRW-A00')
        self.l.s['confirmed']={'KRW-A01':self.l.s['confirmed']['KRW-A01']}
        with self.assertRaisesRegex(RebalanceUnavailable,'모두 보유 중'):self.preview('refill')
        original=copy.deepcopy(self.l.s['scan'])
        for change in ({'status':'SCANNING'},{'status':'DATA_UNAVAILABLE'},
                       {'boundary':T-HOUR},{'bootstrap':True}):
            self.l.s['scan']=dict(original,**change)
            with self.subTest(change=change),self.assertRaisesRegex(RebalanceUnavailable,'스캔이 완료되지'):
                self.preview('rebuild')
            self.assertFalse(self.l.pending())
    def test_rebuild_sells_then_buys_from_settled_cash_and_survives_restart(self):
        initial=self.l.s['initial'];self.run_action('rebuild')
        self.assertEqual({o['side'] for o in self.l.pending().values()},{'SELL'})
        self.fill('KRW-A00',90)
        advance(self.l,self.stamp+STEP+1000)
        self.assertFalse(any(o['side']=='BUY' for o in self.l.pending().values()))
        self.l.db.close();self.l=HourlyLedger(self.tmp.name,self.stamp+STEP+1000)
        for symbol in list(self.l.pending()):self.fill(symbol,105)
        settled=self.l.s['cash'];realized=self.l.s['realized']
        advance(self.l,self.stamp+STEP+2000)
        orders=self.l.pending()
        self.assertEqual(len(orders),10)
        self.assertTrue(all(o['side']=='BUY' for o in orders.values()))
        self.assertAlmostEqual(sum(o['budget'] for o in orders.values()),settled)
        self.assertTrue(all(o['earliest']>self.stamp+STEP+2000 for o in orders.values()))
        for symbol in list(orders):self.fill(symbol)
        advance(self.l,self.stamp+2*STEP+1000)
        self.assertEqual(self.l.s['rebalance']['phase'],'DONE')
        self.assertEqual(self.l.s['initial'],initial)
        self.assertEqual(self.l.s['realized'],realized)
        self.assertEqual(len(self.l.s['positions']),10)
        self.assertAlmostEqual(self.l.s['cash'],0)
        buys=self.l.db.execute("SELECT COUNT(*) FROM events WHERE kind='BUY'").fetchone()[0]
        advance(self.l,self.stamp+2*STEP+2000)
        self.assertEqual(buys,self.l.db.execute("SELECT COUNT(*) FROM events WHERE kind='BUY'").fetchone()[0])
    def test_refill_keeps_holdings_and_reuses_fresh_slot_skipped_candidates(self):
        # Sell one holding; only post-exit newly-confirmed candidates may replace it.
        self.l.schedule_exit('KRW-A00',T+STEP+1,'TEST',T)
        self.fill('KRW-A00',110)
        kept=copy.deepcopy(self.l.s['positions']);cash=self.l.s['cash']
        op=self.run_action('refill')
        self.assertEqual(list(op['targets']),['KRW-A10'])
        self.assertEqual(self.l.s['positions'],kept)
        self.assertEqual(list(self.l.pending()),['KRW-A10'])
        self.assertAlmostEqual(self.l.pending()['KRW-A10']['budget'],cash)
        self.fill('KRW-A10');advance(self.l,self.stamp+STEP+10)
        for symbol,p in kept.items():self.assertEqual(self.l.s['positions'][symbol],p)
    def test_recheck_fingerprint_pending_and_no_vacancy(self):
        prepared=self.preview('rebuild');self.l.s['cash']+=1
        with self.assertRaisesRegex(RebalanceUnavailable,'변경'):request(self.l,'rebuild',self.stamp,prepared['fingerprint'])
        with self.assertRaisesRegex(RebalanceUnavailable,'빈자리'):self.preview('refill')
        self.l.schedule_exit('KRW-A00',self.stamp,'TEST',T)
        with self.assertRaisesRegex(RebalanceUnavailable,'대기'):self.preview('rebuild')
    def test_expiry_during_selling_keeps_cash_and_clear_never_rebuys(self):
        self.run_action('rebuild')
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,T+HOUR)
        self.assertFalse(self.l.pending());self.assertFalse(self.l.s['positions'])
        self.assertEqual(self.l.s['rebalance']['phase'],'CANCELED')
        self.assertGreater(self.l.s['cash'],0)
    def test_clear_interrupts_persistent_operation(self):
        self.run_action('rebuild');self.l.clear(self.stamp+100)
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,self.stamp+STEP+100)
        self.assertFalse(self.l.pending());self.assertFalse(self.l.s['enabled'])
        self.assertEqual(self.l.s['rebalance']['phase'],'CANCELED')
    def test_manual_gap_below_stop_rejected_without_spending(self):
        self.run_action('rebuild')
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,self.stamp+STEP+100)
        symbol=next(iter(self.l.pending()));order=self.l.pending()[symbol];cash=self.l.s['cash']
        self.assertFalse(self.l.execute(symbol,order,[order['earliest'],90],order['earliest']+1))
        self.assertEqual(self.l.s['cash'],cash);self.assertNotIn(symbol,self.l.s['positions'])
        self.assertIn(symbol,self.l.s['rebalance']['canceled'])
    def test_automatic_entries_wait_during_rebuild(self):
        self.run_action('rebuild')
        self.l.observe('KRW-NEW',point(T),T+1000,1)
        self.l.observe('KRW-NEW',point(T+HOUR),T+HOUR+1000,1)
        self.assertTrue(all(o['side']=='SELL' for o in self.l.pending().values()))
    def test_quantity_display_preserves_balance_and_ten_holdings_fit(self):
        before=copy.deepcopy(self.l.s)
        text,_=positions(self.l,self.stamp)
        self.assertIn('수량 2,997개',text)
        self.assertLess(len(text),4096)
        self.assertEqual(self.l.s,before)
    def test_upgrade_restores_candidate_evidence_without_replaying_trades(self):
        before=copy.deepcopy(self.l.s['positions'])
        self.l.s.pop('confirmed');self.l.save();self.l.db.close()
        self.l=HourlyLedger(self.tmp.name,self.stamp)
        self.assertEqual(len(candidates(self.l,self.stamp)),12)
        self.assertEqual(self.l.s['positions'],before)
        self.assertFalse(self.l.pending())
    def test_expired_automatic_order_does_not_require_manual_operation(self):
        order=dict(side='BUY',budget=300000,decision_ms=T)
        self.l.s['pending']['KRW-X']=order
        cash=self.l.s['cash']
        with self.l.lock,self.l.db:
            cancel_entry(self.l,'KRW-X',order,T+HOUR+1,'STALE_UNFILLED')
        self.assertNotIn('KRW-X',self.l.pending())
        self.assertEqual(self.l.s['cash'],cash)
        self.assertNotIn('rebalance',self.l.s)
    def test_manual_entry_cannot_fill_after_approved_hour(self):
        self.run_action('rebuild')
        for symbol in list(self.l.pending()):self.fill(symbol)
        advance(self.l,self.stamp+STEP+100)
        symbol=next(iter(self.l.pending()));order=self.l.pending()[symbol]
        self.assertFalse(self.l.execute(symbol,order,[order['earliest'],100],T+HOUR))
        self.assertNotIn(symbol,self.l.s['positions'])
    def test_ui_preview_confirmation_actor_cancel_and_replay(self):
        self.l.s['confirmed']={k:v for k,v in self.l.s['confirmed'].items() if k<'KRW-A06'}
        class Paper:pass
        paper=Paper();paper.ledger=self.l
        with patch.object(server,'FAST_PAPER',paper), patch.object(server,'ALLOWED_CHAT_ID','7'), \
             patch.object(server,'ALLOWED_USER_IDS',set()),patch.object(server,'CONFIRMATIONS',Confirmations()), \
             patch.object(server,'telegram') as send,patch.object(server,'telegram_api'), \
             patch.object(server.time,'time_ns',return_value=self.stamp*1000000),patch.object(server,'start_engine') as vpd:
            def callback(data,user='7'):
                server.handle_callback(dict(id='x',data=data,**{'from':dict(id=user)},
                    message=dict(message_id=1,chat=dict(id='7'))))
            callback('nav:indicator_rebuild')
            self.assertFalse(self.l.pending())
            self.assertIn('최신 유효 포착 6종목',send.call_args.args[0])
            self.assertIn('나머지 4슬롯 몫은 현금 유지',send.call_args.args[0])
            buttons=send.call_args.args[1]['inline_keyboard'][0]
            callback(buttons[0]['callback_data'],'8');self.assertFalse(self.l.pending())
            callback(buttons[1]['callback_data']);callback(buttons[0]['callback_data']);self.assertFalse(self.l.pending())
            callback('nav:indicator_rebuild');yes=send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
            callback(yes);self.assertEqual(len(self.l.pending()),10)
            count=self.l.db.execute("SELECT COUNT(*) FROM events WHERE kind='REBALANCE_REQUEST'").fetchone()[0]
            callback(yes)
            self.assertEqual(count,self.l.db.execute("SELECT COUNT(*) FROM events WHERE kind='REBALANCE_REQUEST'").fetchone()[0])
            vpd.assert_not_called()


if __name__=='__main__':unittest.main()
