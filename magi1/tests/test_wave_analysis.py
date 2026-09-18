import copy
import json
from pathlib import Path
import tempfile
import unittest
from magi1.storage import Storage
from magi1.wave_analysis import (build_report, publish, read_records, Intervals,
                                 SHIFTS_MS, MIN_CONTROLS, WINDOW_MS)

BASE = 100 * WINDOW_MS
NOW = BASE + 4 * 3600000 - 1


def dataset(actual=True, controls=(False, False, False, False, False), offset=0, ident='main'):
    ts = BASE + 2 * 3600000 + offset
    records = {'flow_state': [], 'reception_trial_v3': [], 'evaluation': [], 'diagnostics': []}

    def episode(key, at, origin, reacted):
        venues = {origin: {'ts_ms': at, 'move_bps': 5}}
        if reacted:
            venues['upbit'] = {'ts_ms': at + 5000, 'move_bps': 4}
        common = dict(shock_id=key, asset='BTC', direction='BUY', horizon='MICRO',
                      origin_venue=origin, origin_feature={'return_bps': 5, 'volume': 10,
                      'signed_volume': 8, 'volume_acceleration': 2, 'spread_bps': 1})
        records['flow_state'] += [dict(common, state='FORMATION', event_ts_ms=at,
                                       venues={origin: dict(venues[origin])}),
                                  dict(common, state='NORMALIZATION', event_ts_ms=at+300000,
                                       origin_ts_ms=at, venues=venues)]
        records['reception_trial_v3'].append(dict(common, follower_venue='upbit',
            origin_ts_ms=at, event_ts_ms=at+30000, status='RECEIVED' if reacted else 'NON_REACTION'))

    episode(ident, ts, 'binance', actual)
    for i, (shift, reacted) in enumerate(zip(SHIFTS_MS, controls)):
        if reacted is not None:
            episode(f'{ident}-control-{i}', ts + shift, 'kraken', reacted)
    records['diagnostics'] = [{'event_ts_ms': ts-1000, 'pair_timing_resolution': {
        'binance->upbit': {'min_resolvable_lead_ms': 250, 'measurement_ready': True}}}]
    return records


def edge(report):
    return next(x for x in report['edges'] if x['origin_venue'] == 'binance')


class AnalysisTests(unittest.TestCase):
    def test_time_shift_detects_excess_and_null_not_prediction(self):
        r = build_report(dataset(controls=(True, False, False, False, False)), NOW)
        e = edge(r)
        self.assertEqual(e['matched_n'], 1)
        self.assertEqual(e['control_windows'], 5)
        self.assertEqual((e['actual_rate'], e['control_rate'], e['lift_pp']), (1, .2, 80))
        self.assertEqual(e['resolved_lag_n'], 1)
        self.assertIsNone(e['calibrated_probability'])
        self.assertFalse(r['execution_eligible'])
        self.assertFalse(r['method']['regime_adjusted'])
        null = edge(build_report(dataset(controls=(True,)*5), NOW))
        self.assertEqual(null['lift_pp'], 0)
        self.assertEqual(null['status'], 'INSUFFICIENT_MATCHED_SAMPLE')

    def test_controls_weight_each_case_equally_not_each_shift(self):
        a = dataset(controls=(True, False, False, False, False))
        b = dataset(actual=False, controls=(True, True, None, None, None), offset=120000, ident='second')
        r = build_report({key: a[key]+b[key] for key in a}, NOW)
        e = edge(r)
        self.assertEqual(e['matched_n'], 2)
        self.assertEqual(e['control_windows'], 7)
        self.assertEqual(e['actual_rate'], .5)
        self.assertEqual(e['control_rate'], .6)
        self.assertAlmostEqual(e['lift_pp'], -10)

    def test_missing_control_data_never_becomes_zero_reaction(self):
        e = edge(build_report(dataset(controls=(None,)*5), NOW))
        self.assertEqual(e['matched_n'], 0)
        self.assertIsNone(e['control_rate'])
        self.assertIsNone(e['lift_pp'])
        r = dataset()
        for row in r['reception_trial_v3'][1:]:
            row['status'] = 'UNOBSERVABLE'
        self.assertEqual(edge(build_report(r, NOW))['matched_n'], 0)
        self.assertEqual(edge(build_report(r, NOW))['exclusions']['INSUFFICIENT_COVERED_CONTROLS'], 1)

    def test_actual_unobservable_and_duplicate_conflicts_are_excluded(self):
        r = dataset()
        r['reception_trial_v3'][0]['status'] = 'UNOBSERVABLE'
        self.assertEqual(edge(build_report(r, NOW))['unobservable'], 1)
        self.assertEqual(edge(build_report(r, NOW))['matched_n'], 0)
        r = dataset()
        r['reception_trial_v3'].append(copy.deepcopy(r['reception_trial_v3'][0]))
        self.assertEqual(edge(build_report(r, NOW))['matched_n'], 1)
        r['reception_trial_v3'][-1]['status'] = 'NON_REACTION'
        report = build_report(r, NOW)
        self.assertEqual(report['counts']['conflicting_trials_excluded'], 1)
        self.assertFalse(any(x['origin_venue'] == 'binance' for x in report['edges']))

    def test_coverage_gaps_are_not_bridged(self):
        intervals = Intervals([(0, 14), (16, 30), (30, 40)])
        self.assertFalse(intervals.covers(0, 30))
        self.assertTrue(intervals.covers(16, 40))
        self.assertFalse(intervals.covers(-1, 1))

    def test_direction_and_horizon_catalogs_do_not_mix(self):
        r = dataset(actual=False)
        ts = r['reception_trial_v3'][0]['origin_ts_ms']
        r['flow_state'].append(dict(r['flow_state'][1], shock_id='other-direction',
            direction='SELL', venues={'upbit': {'ts_ms': ts+5000}}))
        r['flow_state'].append(dict(r['flow_state'][1], shock_id='other-horizon',
            horizon='MACRO', venues={'upbit': {'ts_ms': ts+5000}}))
        self.assertEqual(edge(build_report(r, NOW))['actual_rate'], 0)

    def test_unfinished_and_future_records_cannot_enter_comparison(self):
        r = dataset()
        r['flow_state'][1]['event_ts_ms'] = NOW+1
        e = edge(build_report(r, NOW))
        self.assertEqual(e['matched_n'], 0)
        self.assertEqual(e['exclusions']['UNFINISHED_EPISODE'], 1)

    def test_control_windows_cannot_cross_time_block_or_analysis_window(self):
        r = dataset()
        # Put the actual at 00:01; all controls belong to the previous 4h block.
        delta = 119 * 60000
        for row in r['flow_state']:
            row['event_ts_ms'] -= delta
            if 'origin_ts_ms' in row:
                row['origin_ts_ms'] -= delta
            for hit in row['venues'].values():
                hit['ts_ms'] -= delta
        for row in r['reception_trial_v3']:
            row['origin_ts_ms'] -= delta
            row['event_ts_ms'] -= delta
        self.assertEqual(edge(build_report(r, NOW))['matched_n'], 0)
        # Boundary rejection is separately explicit in the analyzer.
        old = build_report(dataset(), BASE + 2 * 3600000 - 1)
        self.assertEqual(old['counts']['matched_trials'], 0)

    def test_no_subsecond_lead_and_no_future_timing_diagnostics(self):
        r = dataset()
        r['flow_state'][1]['venues']['upbit']['ts_ms'] = r['flow_state'][0]['event_ts_ms'] + 500
        self.assertEqual(edge(build_report(r, NOW))['resolved_lag_n'], 0)
        r = dataset()
        r['diagnostics'][0]['pair_timing_resolution']['binance->upbit']['min_resolvable_lead_ms'] = 6000
        self.assertEqual(edge(build_report(r, NOW))['resolved_lag_n'], 0)
        r['diagnostics'][0]['event_ts_ms'] += 2000
        self.assertEqual(edge(build_report(r, NOW))['resolved_lag_n'], 0)

    def test_price_response_dedups_and_excludes_legacy_sell_and_overlapping_cohorts(self):
        r = dataset()
        row = dict(shock_id='x', event_ts_ms=NOW-1, evaluation_version='quote-v2',
                   cohort='FLOW_ONLY', status='COMPLETE', direction='BUY', horizon_sec=30, forward_return=.01)
        r['evaluation'] = [row, dict(row), dict(row, cohort='FLOW_VPD', forward_return=99),
                          dict(row, evaluation_version='legacy', forward_return=99),
                          dict(row, direction='SELL', forward_return=99)]
        result = build_report(r, NOW)['price_response'][1]
        self.assertEqual((result['n'], result['mean_return_pct']), (1, 1))

    def test_readonly_publish_leaves_all_source_rows_and_checkpoints_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            s = Storage(root)
            for kind, rows in dataset().items():
                for row in rows:
                    s.append(kind, row)
            s.checkpoint('sentinel', {'unchanged': True})
            before = list(s.db.execute('SELECT * FROM records'))
            checkpoints = list(s.db.execute('SELECT * FROM checkpoints'))
            report = publish(root, NOW)
            self.assertEqual(edge(report)['matched_n'], 1)
            self.assertEqual(list(s.db.execute('SELECT * FROM records')), before)
            self.assertEqual(list(s.db.execute('SELECT * FROM checkpoints')), checkpoints)
            self.assertEqual(json.loads((Path(root)/'exports/wave_latest.json').read_text())['schema_version'], report['schema_version'])
            s.close()


if __name__ == '__main__':
    unittest.main()
