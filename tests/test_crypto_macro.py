import math
import unittest
import numpy as np
from research.crypto_macro.model import AsOfFeatures, DAY, Policy, outcomes, run


SPECS=[dict(id='rates',family='macro',max_age_days=3),
       dict(id='equities',family='macro',max_age_days=3),
       dict(id='momentum',family='crypto',max_age_days=1.1)]


def observation(series,ts,value,available=None):
    return dict(series=series,observed_ms=ts,available_ms=ts if available is None else available,
                value=value,source='unit-test://synthetic',quality='RECORDED_ASOF')


def target(day,horizon=1,asset='BTC',value=.01):
    return dict(asset=asset,quote='USD',horizon_days=horizon,decision_ms=day*DAY,
                end_ms=(day+horizon)*DAY,available_ms=(day+horizon)*DAY+1,
                start_price=100,end_price=100*math.exp(value),source='unit-test://synthetic')


def fixture():
    rng=np.random.default_rng(123)
    values=rng.normal(size=(110,3)); observations=[]; targets=[]
    for i,x in enumerate(values,1):
        observations.extend(observation(s['id'],i*DAY,float(v)) for s,v in zip(SPECS,x))
        targets.extend(target(i,h,asset,.015*x[0]+.003*x[2]+float(rng.normal(scale=.002)))
                       for h in (1,7) for asset in ('BTC','ETH'))
    return observations,targets


POLICY=Policy(min_train=20,validation=10,window_days=100,refit_days=10)


class MacroTests(unittest.TestCase):
    def test_later_revision_does_not_rewrite_earlier_information(self):
        specs=[SPECS[0]]
        rows=[observation('rates',DAY,1),observation('rates',DAY,99,5*DAY),
              observation('rates',2*DAY,2,3*DAY)]
        store=AsOfFeatures(rows,specs)
        self.assertEqual(store.at(2*DAY)[0].tolist(),[1])
        self.assertEqual(store.at(3*DAY)[0].tolist(),[2])
        # A revision to an older observation must not replace a newer observation.
        self.assertEqual(store.at(5*DAY)[0].tolist(),[2])

    def test_missing_stale_and_unverified_features_never_become_zero(self):
        store=AsOfFeatures([observation('rates',DAY,1)],SPECS)
        self.assertIsNone(store.at(2*DAY)[0])
        self.assertEqual(set(store.at(5*DAY)[1]),{'rates','equities','momentum'})
        with self.assertRaisesRegex(ValueError,'UNVERIFIED_HISTORY'):
            AsOfFeatures([dict(observation('rates',DAY,1),quality='LATEST_ONLY')],SPECS)

    def test_duplicate_or_invalid_timestamps_and_nonfinite_fail(self):
        row=observation('rates',DAY,1)
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            AsOfFeatures([row,row],SPECS)
        for replacement in ({'available_ms':0},{'value':float('nan')},{'observed_ms':True}):
            with self.assertRaises(ValueError): AsOfFeatures([dict(row,**replacement)],SPECS)

    def test_target_timing_currency_contract_and_duplicates(self):
        self.assertAlmostEqual(outcomes([target(1)])[0]['y'],.01)
        for replacement in ({'end_ms':3*DAY},{'available_ms':DAY},{'start_price':0},{'asset':'SOL'},{'quote':'USDT'}):
            with self.assertRaises(ValueError): outcomes([dict(target(1),**replacement)])
        with self.assertRaisesRegex(ValueError,'DUPLICATE'): outcomes([target(1),target(1)])

    def test_forecasts_purge_future_labels_and_compare_models_on_same_sample(self):
        obs,targets=fixture(); report=run(obs,targets,SPECS,100*DAY,POLICY)
        self.assertTrue(report['forecasts']); self.assertFalse(report['live_enabled'])
        self.assertFalse(report['pressure_index_is_probability'])
        self.assertEqual(report['promotion'],'NOT_EVALUATED')
        for r in report['forecasts']:
            self.assertLess(r['train_max_end_ms'],r['fit_ms'])
            self.assertLessEqual(r['train_max_available_ms'],r['fit_ms'])
            self.assertLessEqual(r['fit_ms'],r['decision_ms'])
            self.assertTrue(all(x['available_ms']<=r['decision_ms'] for x in r['available_inputs']))
            self.assertEqual(set(r['predictions']),{'macro_only','crypto_only','mixed'})
            self.assertTrue(0<=r['pressure_index']<=100)
        self.assertEqual(len(report['metrics']),4)

    def test_future_poisoning_cannot_improve_past_predictions(self):
        obs,targets=fixture(); asof=80*DAY
        before=run(obs,targets,SPECS,asof,POLICY)
        later=obs+[observation('rates',DAY,100000,asof+DAY)]
        altered=[dict(t,end_price=99999999) if t['available_ms']>asof else t for t in targets]
        after=run(later,altered,SPECS,asof,POLICY)
        self.assertEqual(before['forecasts'],after['forecasts'])

    def test_late_outcomes_are_not_scored_or_used_in_training(self):
        obs,targets=fixture()
        targets=[dict(t,available_ms=1000*DAY) for t in targets]
        report=run(obs,targets,SPECS,100*DAY,POLICY)
        self.assertEqual(report['forecasts'],[]); self.assertEqual(report['metrics'],[])

    def test_inner_holdout_is_purged_for_seven_day_labels(self):
        from research.crypto_macro.model import select_model
        rows=[dict(t,x=np.array([float(i),math.sin(i)])) for i,t in enumerate(outcomes([target(d,7) for d in range(1,70)]))]
        m=select_model(rows,[0,1],POLICY)
        self.assertIsNotNone(m)
        self.assertLess(m['inner_train_last_end_ms'],m['inner_validation_start_ms'])

    def test_no_input_does_not_fabricate_scores_or_chart(self):
        report=run([],[],SPECS,100*DAY,POLICY)
        self.assertEqual(report['status'],'DATA_OR_HISTORY_REQUIRED')
        self.assertEqual(report['forecasts'],[])
        from research.crypto_macro.plot import render
        with self.assertRaisesRegex(ValueError,'NO_OBSERVED'):
            render(report,'should-not-be-created.png')


if __name__=='__main__': unittest.main()
