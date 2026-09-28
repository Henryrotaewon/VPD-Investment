import copy
import json
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from magi2 import indicator_risk as risk
from magi2.hourly_indicator import HourlyLedger, HourlyMonitor, HOUR, DAY, STEP
from magi2.hourly_indicator_report import positions, events_view

T=200*DAY+2*HOUR


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.l=HourlyLedger(self.tmp.name,T)
        cost=291988.;price=8440.
        self.l.s['positions']['KRW-EGLD']=dict(entry_ms=T-STEP,cost=cost,
            qty=cost/(price*1.0005**2),reference=price,initial_stop=6535.,
            protection=dict(peak_net_pct=4.29,floor_net_pct=None,allowed_giveback_pp=None))
        self.l.s['cash']-=cost;self.l.save()
        self.pos=self.l.s['positions']['KRW-EGLD']
    def tearDown(self):
        self.l.db.close();self.tmp.cleanup()
    def check(self,price,stamp=T+1000,quote_ms=None):
        return self.l.check_hard_stops({'KRW-EGLD':dict(price=price,ts=stamp if quote_ms is None else quote_ms)},
                                     stamp,self.l.risk_positions())

    def test_egld_existing_loss_triggers_without_profit_activation_or_hourly_scan(self):
        original=copy.deepcopy(self.pos)
        signals,missing=self.check(7320)
        self.assertFalse(missing);self.assertEqual(len(signals),1)
        self.assertAlmostEqual(signals[0]['net_pct'],-13.4434304,places=5)
        self.assertEqual(self.pos,original)
        order=self.l.pending()['KRW-EGLD']
        self.assertEqual(order['reason'],risk.REASON)
        self.assertEqual(order['decision_ms'],T+1000)
        self.assertEqual(order['earliest'],T+STEP)
        self.assertFalse(self.l.execute('KRW-EGLD',order,[T,7949],T+STEP+1))
        self.assertTrue(self.l.execute('KRW-EGLD',order,[T+STEP,7200],T+STEP+1))
        self.assertAlmostEqual(self.l.s['realized'],original['qty']*7200*.9995**2-original['cost'])
        self.assertLess(self.l.s['realized'],-original['cost']*.06)
        self.assertIn('독립 손절',events_view(self.l,T+STEP+1)[0])

    def test_cost_adjusted_threshold_includes_equality_but_not_just_above(self):
        threshold=risk.stop_price(self.pos,self.l.s)
        self.assertGreater(threshold,8440*.94)
        self.assertFalse(self.check(threshold+.001)[0])
        self.assertTrue(self.check(threshold,T+1001)[0])
        self.assertFalse(self.check(threshold-1,T+1002)[0])
        self.assertEqual(self.l.db.execute("SELECT count(*) FROM events WHERE kind='INDEPENDENT_STOP_TRIGGER'").fetchone()[0],1)

    def test_rejects_stale_future_preentry_missing_and_invalid_quotes(self):
        expected=self.l.risk_positions();stamp=T+HOUR
        for quote in ({},{'price':0,'ts':stamp},{'price':float('nan'),'ts':stamp},
                      {'price':7320,'ts':stamp+1},{'price':7320,'ts':stamp-60001},
                      {'price':7320,'ts':T-STEP-1},{'price':7320,'ts':float('inf')}):
            signals,missing=self.l.check_hard_stops({'KRW-EGLD':quote},stamp,expected)
            self.assertFalse(signals);self.assertEqual(missing,['KRW-EGLD'])
        self.assertFalse(self.l.pending())

    def test_no_cached_quote_exit_or_old_quote_against_reentered_position(self):
        self.l.s['prices']['KRW-EGLD']=dict(price=7320,ts=T)
        expected=self.l.risk_positions()
        self.assertFalse(self.l.check_hard_stops({},T+1,expected)[0])
        self.pos['entry_ms']=T+1
        self.assertFalse(self.l.check_hard_stops({'KRW-EGLD':dict(price=7320,ts=T+2)},T+2,expected)[0])
        self.assertFalse(self.l.pending())

    def test_disabled_entries_do_not_disable_risk_and_existing_exit_is_preserved(self):
        self.l.s['enabled']=False
        self.assertTrue(self.check(7320)[0])
        order=copy.deepcopy(self.l.pending()['KRW-EGLD'])
        self.l.schedule_exit('KRW-EGLD',T+2000,'MANUAL_CLEAR',T)
        self.assertEqual(order,self.l.pending()['KRW-EGLD'])

    def test_upgrade_and_restart_preserve_positions_money_orders_and_history(self):
        self.l.s.pop('independent_risk');self.l.s['prices']['KRW-EGLD']=dict(price=7320,ts=T)
        self.l.save();old=copy.deepcopy(self.l.s);self.l.db.close()
        self.l=HourlyLedger(self.tmp.name,T+1000)
        for key in ('positions','cash','realized','pending','started_ms','cohort'):
            self.assertEqual(self.l.s[key],old[key])
        self.assertFalse(self.l.pending())  # migration never acts on cached marks
        self.check(7320,T+2000);order=copy.deepcopy(self.l.pending())
        count=self.l.db.execute("SELECT count(*) FROM events WHERE kind='INDEPENDENT_RISK_ACTIVATED'").fetchone()[0]
        self.l.db.close();self.l=HourlyLedger(self.tmp.name,T+3000)
        self.assertEqual(order,self.l.pending())
        self.assertEqual(count,self.l.db.execute("SELECT count(*) FROM events WHERE kind='INDEPENDENT_RISK_ACTIVATED'").fetchone()[0])

    def test_pending_exit_executes_once_even_if_ticker_fails_and_workers_race(self):
        self.check(7320)
        order=self.l.pending()['KRW-EGLD'];stamp=order['earliest']+1
        market=Mock();market.prices.side_effect=RuntimeError('ticker unavailable')
        market.fills.return_value=[order['earliest'],7200]
        monitor=HourlyMonitor(self.tmp.name,lambda x:None,SimpleNamespace(ledger=self.l),clock=lambda:stamp)
        monitor.risk_cycle(market)
        self.assertFalse(self.l.execute('KRW-EGLD',order,market.fills.return_value,stamp))
        self.assertEqual(self.l.db.execute("SELECT count(*) FROM events WHERE kind='SELL'").fetchone()[0],1)

    def test_risk_worker_runs_while_hourly_worker_is_blocked(self):
        stamp=T+1000;triggered=threading.Event();scanning=threading.Event()
        market=Mock();market.prices.return_value={'KRW-EGLD':dict(price=7320,ts=stamp)}
        monitor=HourlyMonitor(self.tmp.name,lambda x:triggered.set() if x.startswith('indicator_independent_stop ') else None,
            SimpleNamespace(ledger=self.l),market_factory=lambda:market,clock=lambda:stamp)
        def blocked_scan():
            scanning.set();monitor.stop.wait(3)
        monitor.worker=blocked_scan
        monitor.start()
        try:
            self.assertTrue(scanning.wait(2));self.assertTrue(triggered.wait(2))
            self.assertTrue(monitor.thread.is_alive())
        finally:
            monitor.stop.set();monitor.thread.join(3);monitor.risk_thread.join(3)
        self.assertIn('KRW-EGLD',self.l.pending())

    def test_existing_exit_is_not_delayed_by_hourly_scan_or_replaced(self):
        self.l.schedule_exit('KRW-EGLD',T+1000,'HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE',T)
        order=self.l.pending()['KRW-EGLD'];stamp=order['earliest']+1
        market=Mock();market.prices.return_value={'KRW-EGLD':dict(price=7200,ts=stamp)}
        market.fills.return_value=[order['earliest'],7200]
        monitor=HourlyMonitor(self.tmp.name,lambda x:None,SimpleNamespace(ledger=self.l),clock=lambda:stamp)
        monitor.risk_cycle(market)
        self.assertFalse(self.l.pending());self.assertFalse(self.l.s['positions'])
        payload=json.loads(self.l.db.execute("SELECT payload FROM events WHERE kind='SELL'").fetchone()[0])
        self.assertEqual(payload['reason'],order['reason'])
        self.assertEqual(payload['decision_ms'],order['decision_ms'])

    def test_report_distinguishes_hourly_line_and_independent_stop_without_mutation(self):
        for i in range(1,10):self.l.s['positions'][f'KRW-X{i}']=copy.deepcopy(self.pos)
        before=copy.deepcopy(self.l.s)
        for offset in (0,5):
            text,_=positions(self.l,T,offset)
            self.assertLess(len(text),4096)
            self.assertEqual(text.count('독립 손절선'),5)
            self.assertIn('−6%',text)
        self.assertEqual(before,self.l.s)


if __name__=='__main__':unittest.main()
