import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, Mock

from magi2 import paper_engine as engine
from magi2.hold_policy import POLICY, assess_hold, protection

NOW = datetime.fromisoformat('2026-09-19T07:30:00+09:00')
ASOF = NOW - timedelta(minutes=8)


def position(**kw):
    return dict(dict(market='KRW-X', status='OPEN', qty=1., cost_krw=100.,
                     entry_price=100., entry_market_price=100., peak_price=100.,
                     target_profit_pct=12., stop_loss_pct=-6.), **kw)


def row(**kw):
    return dict(dict(coin='X', market='KRW-X', Rank=1, VPD=18, momentum='↑',
                     **{'TodayValue/10': .2, 'IntraAccel': .2}), **kw)


class HoldPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(engine.CFG)
        self.cfg['fee_rate'] = 0

    def test_low_vpd_winner_uses_profit_protection_not_entry_score(self):
        p = position(peak_price=110)
        original = copy.deepcopy(p)
        self.assertEqual(assess_hold(row(),p,108,self.cfg)['kind'], 'PROFIT')
        self.assertEqual(p, original)
        self.assertEqual(assess_hold(row(),position(),101,self.cfg)['kind'], 'WEAKENING')

    def test_neutral_top_rank_and_high_score_are_not_automatic_keep(self):
        r = row(VPD=92, momentum='→', **{'TodayValue/10': 4., 'IntraAccel': 7.})
        self.assertEqual(assess_hold(r,position(),101,self.cfg)['kind'], 'WEAKENING')

    def test_trend_needs_volume_support_not_giveback_safety(self):
        self.assertEqual(assess_hold(row(**{'Giveback%p':0}),position(),101,self.cfg)['kind'], 'WEAKENING')
        self.assertEqual(assess_hold(row(**{'IntraAccel':1.1}),position(),101,self.cfg)['kind'], 'TREND')

    def test_protection_has_priority_over_healthy_trend(self):
        r=row(**{'IntraAccel':2})
        p=position(peak_price=110)
        self.assertEqual(assess_hold(r,p,106,self.cfg)['kind'], 'PROFIT_PROTECTION')
        self.assertEqual(assess_hold(r,p,90,self.cfg)['kind'], 'HARD_STOP')
        self.assertEqual(assess_hold(r,p,113,self.cfg)['kind'], 'TAKE_PROFIT')

    def test_bad_data_does_not_mean_signal_decay(self):
        for px in [None,0,-1,float('nan'),float('inf')]:
            self.assertEqual(assess_hold(row(),position(),px,self.cfg)['kind'], 'DATA_WAIT')
        self.assertEqual(assess_hold({},position(),101,self.cfg)['kind'], 'DATA_WAIT')
        self.assertEqual(assess_hold(row(**{'IntraAccel':'nan'}),position(),101,self.cfg)['kind'], 'DATA_WAIT')

    def test_protection_uses_fee_adjusted_profit_not_day_high(self):
        cfg=copy.deepcopy(self.cfg); cfg['fee_rate']=.01
        r=protection(position(peak_price=106),103,cfg)
        self.assertLess(r['peak_net_return_pct'],6)
        self.assertIsNone(r['kind'])

    def test_exact_protection_boundary(self):
        self.assertEqual(protection(position(peak_price=106),103,self.cfg)['kind'],'PROFIT_PROTECTION')


class RebalanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg=copy.deepcopy(engine.CFG)
        self.cfg['session']['top_n']=1
        self.cfg['fee_rate']=0
        self.patches=[patch.object(engine,'CFG',self.cfg),patch.object(engine,'now_dt',return_value=NOW),
                      patch.object(engine,'save_state'),patch.object(engine,'log_event'),
                      patch.object(engine,'telegram'),patch.object(engine,'send_current_status'),
                      patch.object(engine,'get_prices',return_value={'KRW-X':101}),
                      patch.object(engine,'load_all_ranked_rows',return_value={'X':row(momentum='→')})]
        mocks=[p.start() for p in self.patches]
        self.clock,self.save,self.log,self.send,self.status,self.prices,self.rows=mocks[1:]
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        self.st={'positions':{'X':position()},'cash_krw':0,'initial_cash_krw':100}

    def run_at(self,asof,now=None):
        self.clock.return_value=now or asof+timedelta(minutes=8)
        snap={'schema':engine.SCHEMA,'basis':'POINT_IN_TIME','asof':asof.isoformat(),
              'generated_at':asof.isoformat(),'top10':[row()],'all_rows':self.rows.return_value}
        with patch.object(engine,'load_morning_snapshot',return_value=(snap,asof)):
            return engine.morning_rebalance(self.st)

    def test_weakening_requires_elapsed_time_and_never_immediate_rebuy(self):
        self.assertTrue(self.run_at(ASOF))
        self.assertIn('매도 유예',self.send.call_args.args[0])
        self.run_at(ASOF+timedelta(minutes=2))
        self.assertEqual(self.st['positions']['X']['weakening_count'],2)
        self.assertEqual(self.st['positions']['X']['status'],'OPEN')
        self.run_at(ASOF+timedelta(days=1))
        self.assertEqual(self.st['positions']['X']['status'],'CLOSED')
        self.assertEqual(self.st['positions']['X']['exit_reason'],'VPD_SIGNAL_DECAY')
        self.assertGreater(self.st['cash_krw'],0)

    def test_duplicate_snapshot_does_not_change_state(self):
        self.run_at(ASOF)
        before=copy.deepcopy(self.st)
        self.assertFalse(self.run_at(ASOF))
        self.assertEqual(self.st,before)

    def test_legacy_counts_start_new_policy_observation(self):
        self.st['positions']['X'].update(weakening_count=10,weakening_since='2020-01-01T07:00:00+09:00')
        self.run_at(ASOF)
        self.assertEqual(self.st['positions']['X']['status'],'OPEN')
        self.assertEqual(self.st['positions']['X']['weakening_count'],1)

    def test_missing_price_does_not_consume_grace(self):
        self.prices.return_value={}
        self.st['positions']['X'].update(weakening_count=1,weakening_policy=POLICY,
                                         weakening_since=(ASOF-timedelta(days=2)).isoformat())
        self.run_at(ASOF)
        self.assertEqual(self.st['positions']['X']['weakening_count'],1)
        self.assertEqual(self.st['positions']['X']['status'],'OPEN')
        self.assertIn('자료 대기 1',self.send.call_args.args[0])

    def test_missing_data_can_retry_same_snapshot_after_recovery(self):
        self.prices.return_value={}
        before=copy.deepcopy(self.st)
        self.assertFalse(self.run_at(ASOF))
        self.assertEqual(self.st,before)
        self.prices.return_value={'KRW-X':101}
        self.assertTrue(self.run_at(ASOF))
        self.assertEqual(self.st['positions']['X']['weakening_count'],1)

    def test_stale_and_future_inputs_do_not_mutate_or_trade(self):
        for asof,now in [(ASOF-timedelta(days=1),NOW),(ASOF,NOW+timedelta(hours=1)),
                         (ASOF+timedelta(minutes=20),NOW)]:
            before=copy.deepcopy(self.st)
            self.assertFalse(self.run_at(asof,now))
            self.assertEqual(self.st,before)
        self.prices.assert_not_called()

    def test_fresh_scan_is_accepted_at_every_hour(self):
        for hour in range(24):
            self.assertTrue(self.run_at(ASOF.replace(hour=hour)))

    def test_monitor_saves_new_peak_even_without_warning(self):
        self.prices.return_value={'KRW-X':104}
        engine.monitor_once(self.st)
        self.save.assert_called_once()
        self.assertEqual(self.st['positions']['X']['peak_price'],104)
        self.send.assert_not_called()

    def test_monitor_protects_profit_between_daily_rebalances(self):
        self.st['positions']['X']['peak_price']=110
        self.prices.return_value={'KRW-X':106}
        engine.monitor_once(self.st)
        self.assertEqual(self.st['positions']['X']['exit_reason'],'PROFIT_PROTECTION')

    def test_recovered_trend_resets_weakening_clock(self):
        self.run_at(ASOF)
        self.rows.return_value={'X':row(**{'IntraAccel':2})}
        self.run_at(ASOF+timedelta(minutes=2))
        self.assertEqual(self.st['positions']['X']['weakening_count'],0)
        self.assertNotIn('weakening_since',self.st['positions']['X'])
        self.assertIn('추세 유지',self.send.call_args.args[0])



if __name__=='__main__':unittest.main()
