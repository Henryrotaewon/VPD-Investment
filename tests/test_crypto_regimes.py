import copy
import unittest
import numpy as np
from research.crypto_macro.model import DAY
from research.crypto_macro.regimes import (RegimePolicy, classify, macro_directions,
    rebalance, portfolio_step, simulate, select_strategy, metrics, weights, CAPS)

P=RegimePolicy(trend_days=5,vol_days=3,vol_reference_days=6,confirmation_days=2,
               selection_window_days=30,minimum_regime_samples=3,selection_refit_days=3)


def panel(n=100):
    rng=np.random.default_rng(41)
    returns=rng.normal(.001,.025,(n,2))
    prices=100*np.exp(np.cumsum(returns,axis=0))
    return [dict(ts_ms=(i+1)*DAY,prices=x.tolist()) for i,x in enumerate(prices)]


class RegimeTests(unittest.TestCase):
    def test_buy_sell_costs_are_self_financing(self):
        holdings,fee,turnover=rebalance([0,0,1000],[.5,.5],.01)
        self.assertAlmostEqual(holdings.sum(),1000/1.01,8)
        self.assertAlmostEqual(1000-holdings.sum(),fee,8)
        cash,fee2,turnover2=rebalance(holdings,[0,0],.01)
        self.assertAlmostEqual(cash.sum(),1000/1.01*.99,8)
        self.assertAlmostEqual(turnover2,holdings[:2].sum(),8)
        self.assertGreater(fee2,0)

    def test_drift_rebalance_and_no_free_cash(self):
        marked,_=portfolio_step([0,0,1],[.25,.25],[1,-.5],0)
        self.assertAlmostEqual(marked.sum(),1.125)
        next_hold,fee,_=rebalance(marked,[.25,.25],.0015)
        self.assertAlmostEqual(next_hold.sum()+fee,marked.sum(),12)
        self.assertGreaterEqual(next_hold[2],0)
        for target in ([-.1,.1],[1,1],[float('nan'),0]):
            with self.assertRaises(ValueError):rebalance(marked,target,.0015)

    def test_two_day_lag_and_future_poisoning(self):
        rows=panel(); signals=classify(rows,P)
        poison=copy.deepcopy(rows)
        for row in poison[60:]:row['prices']=[1e6,1e7]
        changed=classify(poison,P)
        self.assertEqual(signals[:62],changed[:62])
        before=simulate(rows,signals,40*DAY,15,P)
        after=simulate(poison,changed,40*DAY,15,P)
        self.assertEqual([r for r in before if r['end_ms']<=60*DAY],
                         [r for r in after if r['end_ms']<=60*DAY])

    def test_all_four_regimes_use_only_past_volatility(self):
        rows=panel(300); signals=[x for x in classify(rows,P) if x]
        self.assertEqual(set(s['regime'] for s in signals),set(CAPS))
        for s in signals:
            self.assertEqual(s['decision_ms']-s['input_cutoff_ms'],2*DAY)
            self.assertLessEqual(s['risk_cap'],CAPS[s['regime']])
            self.assertLessEqual(s['risk_cap'],CAPS[s['raw_regime']])
            for name in ('cash','equal','momentum','inverse_vol'):
                w=weights(name,s);self.assertTrue(np.all(w>=0));self.assertLessEqual(w.sum(),s['risk_cap']+1e-12)

    def test_strategy_selection_cannot_see_current_or_unfinished_return(self):
        def entry(start,end,gain):
            return dict(decision_ms=start*DAY,end_ms=end*DAY,regime='UP_LOW',
                candidates={s:{'net_return':gain if s=='momentum' else 0} for s in ('cash','equal','momentum','inverse_vol')})
        history=[entry(i,i+1,.01) for i in range(1,6)]
        before=select_strategy(history,10*DAY,'UP_LOW',P)
        after=select_strategy(history+[entry(9,10,10),entry(10,11,20)],10*DAY,'UP_LOW',P)
        self.assertEqual(before,after);self.assertEqual(before['strategy'],'momentum')
        losses=[entry(i,i+1,-.01) for i in range(1,6)]
        self.assertEqual(select_strategy(losses,10*DAY,'UP_LOW',P)['strategy'],'cash')

    def test_drawdown_includes_initial_capital_and_period_boundaries(self):
        rows=[{'portfolios':{'x':{'net_return':r,'exposure':1,'turnover':0}}} for r in (-.2,.25)]
        m=metrics(rows,'x');self.assertAlmostEqual(m['max_drawdown'],-.2)
        self.assertAlmostEqual(m['total_return'],0);self.assertIsNone(m['cagr'])

    def test_missing_daily_data_does_not_disappear_from_backtest(self):
        rows=panel()
        with self.assertRaisesRegex(ValueError,'CONTIGUOUS'):classify(rows[:30]+rows[31:],P)
        signals=classify(rows,P);signals[60]=None
        with self.assertRaisesRegex(ValueError,'MISSING_REGIME'):simulate(rows,signals,40*DAY,15,P)

    def test_macro_input_validates_issue_time_and_ignores_actuals(self):
        row=dict(asset='BTC',horizon_days=7,decision_ms=20*DAY,fit_ms=19*DAY,
            train_max_end_ms=18*DAY,train_max_available_ms=18*DAY,
            available_inputs=[dict(observed_ms=19*DAY,available_ms=20*DAY)],predictions={'mixed':.1})
        report={'mode':'RESEARCH_ONLY','forecasts':[row]}
        before=macro_directions(report)
        row['actual_log_return']=999
        self.assertEqual(before,macro_directions(report))
        row['train_max_end_ms']=21*DAY
        with self.assertRaisesRegex(ValueError,'NONCAUSAL'):macro_directions(report)

    def test_cost_stress_reduces_fixed_benchmark_and_selection_is_causal(self):
        rows=panel();signals=classify(rows,P)
        cheap=simulate(rows,signals,40*DAY,15,P);costly=simulate(rows,signals,40*DAY,30,P)
        self.assertLess(costly[-1]['portfolios']['fixed_50']['nav'],cheap[-1]['portfolios']['fixed_50']['nav'])
        for r in cheap:
            s=r['selection']
            if s['last_outcome_ms'] is not None:self.assertLess(s['last_outcome_ms'],s['fitted_ms'])
            self.assertLessEqual(s['fitted_ms'],r['decision_ms'])
            self.assertAlmostEqual(sum(r['target_weights'].values()),1)


if __name__=='__main__':unittest.main()
