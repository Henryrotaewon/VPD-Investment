import copy
import tempfile
import unittest
from unittest.mock import patch

from magi2 import indicator_protection as protection
from magi2.hourly_indicator import HourlyLedger, HOUR, DAY, STEP, PublicCandles
from magi2.hourly_indicator_report import positions, events_view, protection_lines
from magi2.indicator_rebalance import candidates, plan, request, advance
from magi2 import server_runner as server

T=200*DAY+2*HOUR


def point(t, close=100, atr=1, trend=False):
    return dict(boundary=t,day=(t-1)//DAY*DAY,qualifies=True,close=close,
                low=95,trend_exit=trend,atr_1h=atr)


class AtrTests(unittest.TestCase):
    def test_wilder_true_range_includes_gaps_and_ignores_unfinished_hour(self):
        rows=[[T+i*HOUR,100,101,99,100,1] for i in range(15)]
        b=T+15*HOUR
        self.assertEqual(protection.hourly_atr(rows,b),2)
        rows.append([b,110,111,109,110,1])
        self.assertEqual(protection.hourly_atr(rows,b),2)
        self.assertAlmostEqual(protection.hourly_atr(rows,b+HOUR),(2*13+11)/14)
        self.assertIsNone(protection.hourly_atr(rows,b+2*HOUR))
        self.assertIsNone(protection.hourly_atr(rows[:8]+rows[9:],b+HOUR))
        self.assertIsNone(protection.hourly_atr(rows[:14],T+14*HOUR))

    def test_fetch_has_hourly_atr_warmup_without_additional_api_requests(self):
        market=PublicCandles();day=(T-1)//DAY*DAY
        market.cache['A']=dict(day=day,rows=[])
        with patch.object(market,'get',return_value=[]) as get, \
             patch('magi2.hourly_indicator.recovery_point',return_value={}) as calc:
            market.observe('A',T)
        self.assertEqual(get.call_count,1)
        self.assertEqual(get.call_args.args[1]['count'],200)
        calc.assert_called_once_with([],[],T)


class ProtectionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.l=HourlyLedger(self.tmp.name,T-HOUR-1)
        self.l.observe('A',point(T-HOUR),T-HOUR+1000,1)
        self.l.observe('A',point(T),T+1000,1)
        self.fill('A',100)
        self.pos=self.l.s['positions']['A']
        self.base=protection.breakeven_price(self.pos,self.l.s)
        self.hour=0
    def tearDown(self):
        self.l.db.close();self.tmp.cleanup()
    def fill(self,symbol,price):
        order=self.l.pending()[symbol]
        self.assertTrue(self.l.execute(symbol,order,[order['earliest'],price],order['earliest']+1))
    def observe_return(self,ret,atr_pp=1,trend=False):
        self.hour+=1;b=T+self.hour*HOUR
        self.l.observe('A',point(b,self.base*(1+ret/100),None if atr_pp is None else self.base*atr_pp/100,trend),b+1000,1)
        return self.pos['protection']
    def restart(self,stamp):
        self.l.db.close();self.l=HourlyLedger(self.tmp.name,stamp)
        self.pos=self.l.s['positions']['A']

    def test_breakeven_uses_all_costs_and_one_close_exits_next_bar(self):
        self.assertAlmostEqual(self.base,100*1.0005**2/.9995**2)
        self.assertIsNone(self.observe_return(5.99)['floor_net_pct'])
        self.assertEqual(self.observe_return(6)['floor_net_pct'],0)
        self.assertFalse(self.l.pending())  # activation is not a take-profit
        self.observe_return(0)
        self.assertFalse(self.l.pending())  # equality is not a breach
        self.observe_return(-.01)
        order=self.l.pending()['A']
        self.assertEqual(order['reason'],'HOURLY_BREAKEVEN_PROTECTION')
        self.assertEqual(order['earliest'],T+4*HOUR+STEP)
        self.assertFalse(self.l.execute('A',order,[order['signal_ms'],self.base],order['earliest']+1))
        cost=self.pos['cost'];qty=self.pos['qty']
        self.fill('A',99)
        self.assertAlmostEqual(self.l.s['realized'],qty*99*.9995**2-cost)
        self.assertLess(self.l.s['realized'],0)  # the line is not a guaranteed fill
        self.assertIn('본전 보호선 이탈',events_view(self.l,order['earliest']+1)[0])

    def test_trailing_activation_ratchet_atr_and_full_exit(self):
        self.assertAlmostEqual(self.observe_return(14.99)['floor_net_pct'],0)
        self.assertAlmostEqual(self.observe_return(15)['floor_net_pct'],9)
        self.assertFalse(self.l.pending())
        self.assertAlmostEqual(self.observe_return(20)['floor_net_pct'],14)
        # Volatility grows: raw allowance widens to 9pp, existing +14% stays.
        p=self.observe_return(19,atr_pp=3)
        self.assertAlmostEqual(p['allowed_giveback_pp'],9)
        self.assertAlmostEqual(p['floor_net_pct'],14)
        self.assertIn('최고→보호선 6.00%p','\n'.join(protection_lines(self.pos,self.l.s)))
        before=copy.deepcopy(p);self.restart(T+4*HOUR+2000)
        self.assertEqual(before,self.pos['protection'])
        self.observe_return(13.9,atr_pp=3)
        self.assertEqual(self.l.pending()['A']['reason'],'HOURLY_PROFIT_TRAILING')
        self.fill('A',self.base*1.13)
        self.assertFalse(self.l.s['positions'])
        self.assertIn('수익 추적 보호선 이탈',events_view(self.l,T+5*HOUR+STEP+1)[0])

    def test_atr_converts_price_range_to_net_percentage_points(self):
        p=self.observe_return(20,atr_pp=3)
        self.assertAlmostEqual(p['floor_net_pct'],11)
        self.assertAlmostEqual(p['allowed_giveback_pp'],9)
        self.assertAlmostEqual(protection.effective_stop(self.pos,self.l.s),self.base*1.11)

    def test_ticker_high_and_intrahour_high_do_not_activate_or_raise_peak(self):
        self.l.mark({'A':dict(price=200,ts=T+2*STEP)},T+2*STEP)
        self.assertIsNone(self.pos['protection']['peak_net_pct'])
        b=T+HOUR
        p=point(b,self.base*1.02);p['high']=200
        self.l.observe('A',p,b+1000,1)
        self.assertAlmostEqual(self.pos['protection']['peak_net_pct'],2)
        self.assertIsNone(self.pos['protection']['floor_net_pct'])
        protection.update(self.pos,self.l.s,point(T,200))
        protection.update(self.pos,self.l.s,point(b,200))
        self.assertAlmostEqual(self.pos['protection']['peak_net_pct'],2)

    def test_missing_atr_keeps_existing_floor_and_peak_without_inventing_gap(self):
        p=self.observe_return(20,atr_pp=None)
        self.assertEqual(p['floor_net_pct'],0)
        self.assertIsNone(p['allowed_giveback_pp'])
        self.assertIn('ATR 자료 대기','\n'.join(protection_lines(self.pos,self.l.s)))
        self.assertAlmostEqual(self.observe_return(19,atr_pp=3)['floor_net_pct'],11)
        p=self.observe_return(18,atr_pp=None)
        self.assertAlmostEqual(p['floor_net_pct'],11)
        self.observe_return(10,atr_pp=None)
        self.assertEqual(self.l.pending()['A']['reason'],'HOURLY_PROFIT_TRAILING')

    def test_initial_and_daily_exits_still_apply_before_profit_activation(self):
        self.observe_return(1,trend=True)
        self.assertEqual(self.l.pending()['A']['reason'],'DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK')
        self.l.s['pending'].clear()
        self.observe_return(-10)
        self.assertEqual(self.l.pending()['A']['reason'],'HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE')

    def test_legacy_upgrade_restores_recorded_peak_without_replaying_orders(self):
        self.pos.pop('protection')
        history=[point(T-HOUR,300),point(T+HOUR,self.base*1.20),point(T+2*HOUR,self.base*.99)]
        for p in history:
            p.pop('atr_1h')
            self.l.event(p['boundary']+1000,'OBSERVATION','A',p)
        self.l.s['previous']['A']=history[-1]
        # A future-dated observation is not migration evidence.
        self.l.event(T+10*HOUR,'OBSERVATION','A',point(T+10*HOUR,400))
        before=copy.deepcopy(self.l.s);self.l.save();self.restart(T+2*HOUR+2000)
        self.assertAlmostEqual(self.pos['protection']['peak_net_pct'],20)
        self.assertEqual(self.pos['protection']['floor_net_pct'],0)
        for key in ('cash','realized','pending','used_day','last_exit'):
            self.assertEqual(self.l.s[key],before[key])
        self.assertFalse(self.l.pending())
        restored=copy.deepcopy(self.pos);self.restart(T+2*HOUR+3000)
        self.assertEqual(self.pos,restored)
        self.assertEqual(self.l.db.execute("SELECT COUNT(*) FROM events WHERE kind='PROTECTION_UPGRADE'").fetchone()[0],1)
        self.hour=2;self.observe_return(10,atr_pp=1)
        self.assertEqual(self.l.pending()['A']['reason'],'HOURLY_PROFIT_TRAILING')

    def test_upgrade_without_evidence_and_existing_order_preserved(self):
        self.pos.pop('protection')
        self.l.schedule_exit('A',T+2*STEP,'MANUAL_CLEAR',T)
        pending=copy.deepcopy(self.l.pending());self.l.save();self.restart(T+3*STEP)
        self.assertIsNone(self.pos['protection']['peak_net_pct'])
        self.assertEqual(pending,self.l.pending())

    def test_manual_rebuild_carries_absolute_stop_but_resets_new_trade_peak(self):
        self.observe_return(20)
        stop=protection.effective_stop(self.pos,self.l.s)
        b=T+HOUR
        # Complete, current pool of ten confirmed candidates.
        self.l.s['scan']=dict(status='MONITORING',boundary=b,bootstrap=False,names={})
        for symbol in ['A']+[f'B{i}' for i in range(9)]:
            p=point(b,self.base*1.20)
            self.l.s['previous'][symbol]=p;self.l.s['confirmed'][symbol]=p
            self.l.s['scan']['names'][symbol]=symbol
        self.assertAlmostEqual(candidates(self.l,b+2000)['A']['initial_stop'],stop)
        prepared=plan(self.l,'rebuild',b+2000)
        request(self.l,'rebuild',b+2000,prepared['fingerprint'])
        self.fill('A',self.base*1.20);advance(self.l,b+STEP+1000)
        self.assertAlmostEqual(self.l.pending()['A']['initial_stop'],stop)
        self.fill('A',self.base*1.20)
        self.assertAlmostEqual(self.l.s['positions']['A']['initial_stop'],stop)
        self.assertIsNone(self.l.s['positions']['A']['protection']['peak_net_pct'])

    def test_ui_ten_positions_paginated_read_only_and_callback_routes(self):
        self.observe_return(20)
        for i in range(1,10):self.l.s['positions'][f'KRW-A{i}']=copy.deepcopy(self.pos)
        before=copy.deepcopy(self.l.s)
        first,keys=positions(self.l,T+HOUR+1000)
        second,keys2=positions(self.l,T+HOUR+1000,5)
        for text in (first,second):
            self.assertLess(len(text),4096)
            for label in ('최고 순수익률','현재 보호선','허용 반납폭'):
                self.assertEqual(text.count(label),5)
        self.assertIn('다음 ▶',str(keys));self.assertIn('◀ 이전',str(keys2))
        self.assertEqual(before,self.l.s)
        class Paper:pass
        paper=Paper();paper.ledger=self.l
        with patch.object(server,'FAST_PAPER',paper),patch.object(server,'ALLOWED_CHAT_ID','7'), \
             patch.object(server,'telegram') as send,patch.object(server,'telegram_api'):
            server.handle_callback(dict(id='test',data='fast_results:5',
                message=dict(chat=dict(id='7')),**{'from':dict(id='7')}))
        self.assertIn('보유 상세 2/2페이지',send.call_args.args[0])


if __name__=='__main__':unittest.main()
