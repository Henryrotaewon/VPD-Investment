from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from magi2.fast_wave_indicator import (Candle, DAY_MS, KEYS, VERSION, derivatives,
                                       ema, evaluate, indicators, snapshot)
from magi2.fast_wave_store import EvidenceStore, state_root


def history():
    return [Candle(i * DAY_MS, 110, 90, 100, 1000) for i in range(120)]


def points():
    baseline = dict(zip(KEYS, (0., 50., -60., 1.)))
    out = []
    for i, value in enumerate((0., 1., 3.)):
        out.append(dict(strategy_version=VERSION, venue='upbit', symbol='KRW-TEST',
                        day_ms=120 * DAY_MS, baseline_ms=119 * DAY_MS,
                        baseline=baseline.copy(), observed_ms=120 * DAY_MS + (10 + i) * 60000,
                        current={k: baseline[k] + value * scale for k, scale in zip(KEYS, (1, 1, 2, .1))}))
    return out


def flow(ts):
    return dict(observed_ms=ts, sufficient=True, sample_trades=30, sample_span_ms=6000,
                latest_trade_age_ms=100, buyer_share_pct=75, spread_bps=5)


class IndicatorTests(unittest.TestCase):
    def test_known_flat_and_trending_series(self):
        self.assertEqual(ema([1, 2, 3, 4], 3), [None, None, 2., 3.])
        self.assertEqual(indicators(history()), dict(macd_hist=0., rsi=50., williams=-50.))
        rows = [Candle(i * DAY_MS, 100 + i, 99 + i, 100 + i, 1) for i in range(120)]
        rising = indicators(rows)
        self.assertEqual(rising['rsi'], 100.)
        self.assertEqual(rising['williams'], 0.)
        self.assertAlmostEqual(rising['macd_hist'], 0.)

    def test_current_cannot_change_previous_day_baseline(self):
        rows = history()
        now = 120 * DAY_MS + DAY_MS // 2
        a = snapshot(rows, Candle(120 * DAY_MS, 110, 90, 100, 500), now, venue='upbit', symbol='KRW-T')
        b = snapshot(rows, Candle(120 * DAY_MS, 115, 90, 113, 1500), now, venue='upbit', symbol='KRW-T')
        self.assertEqual(a['baseline'], b['baseline'])
        self.assertEqual(a['current']['volume_pace'], 1.)
        self.assertEqual(b['current']['volume_pace'], 3.)
        self.assertNotEqual(a['current']['rsi'], b['current']['rsi'])
        self.assertEqual(a['baseline_ms'], 119 * DAY_MS)

    def test_missing_duplicate_future_and_insufficient_data_fail(self):
        rows = history()
        current = Candle(120 * DAY_MS, 110, 90, 100, 500)
        for invalid in (rows[:-1], rows + [current], rows[:30], rows[:60] + rows[61:], rows + [rows[-1]]):
            with self.assertRaises(ValueError):
                snapshot(invalid, current, 120 * DAY_MS + 600000, venue='upbit', symbol='KRW-T')
        with self.assertRaises(ValueError):
            snapshot(rows, current, 120 * DAY_MS - 1, venue='upbit', symbol='KRW-T')
        with self.assertRaises(ValueError):
            snapshot(rows, replace(current, close=float('nan')), 120 * DAY_MS + 600000,
                     venue='upbit', symbol='KRW-T')

    def test_unequal_interval_quadratic_has_exact_acceleration(self):
        rows = points()
        for row, minute in zip(rows, (0, 1, 3)):
            row['observed_ms'] = minute * 60000
            row['current'] = {k: minute ** 2 for k in KEYS}
        velocity, acceleration = derivatives(rows)
        for key in KEYS:
            self.assertEqual(velocity[key], 4.)
            self.assertEqual(acceleration[key], 2.)

    def test_linear_rise_is_not_acceleration(self):
        rows = points()
        rows[-1]['current'] = {k: 2 * rows[1]['current'][k] - rows[0]['current'][k] for k in KEYS}
        result = evaluate(rows, rows[-1]['observed_ms'])
        self.assertFalse(result['technical_candidate'])
        self.assertFalse(any(result['accelerating'].values()))

    def test_candidate_requires_fresh_flow(self):
        rows = points()
        now = rows[-1]['observed_ms']
        result = evaluate(rows, now)
        self.assertTrue(result['technical_candidate'])
        self.assertFalse(result['paper_candidate'])
        self.assertTrue(evaluate(rows, now, flow=flow(now))['paper_candidate'])
        for changes in ({'observed_ms': now - 3001}, {'sufficient': False}, {'spread_bps': 11},
                        {'buyer_share_pct': 69}, {'sample_trades': 19}, {'observed_ms': now + 1}):
            self.assertFalse(evaluate(rows, now, flow={**flow(now), **changes})['paper_candidate'])

    def test_restart_rollover_and_stale_points_cannot_trigger(self):
        rows = points()
        now = rows[-1]['observed_ms']
        self.assertFalse(evaluate(rows[:2], now)['ready'])
        self.assertFalse(evaluate(rows, now + 90001)['ready'])
        for key, value in (('day_ms', 121 * DAY_MS), ('symbol', 'KRW-OTHER'), ('baseline_ms', 0)):
            mixed = deepcopy(rows)
            mixed[-1][key] = value
            self.assertFalse(evaluate(mixed, now)['ready'])
        mixed = deepcopy(rows)
        mixed[-1]['observed_ms'] += 300000
        self.assertFalse(evaluate(mixed, mixed[-1]['observed_ms'])['ready'])

    def test_overheated_candidate_rejected(self):
        rows = points()
        rows[-1]['current']['rsi'] = 80
        self.assertFalse(evaluate(rows, rows[-1]['observed_ms'])['technical_candidate'])

    def test_revised_cumulative_volume_is_not_a_new_surge(self):
        rows = points()
        for row, value in zip(rows, (100, 90, 120)):
            row['current_volume'] = value
        self.assertEqual(evaluate(rows, rows[-1]['observed_ms'])['reason'], 'CUMULATIVE_VOLUME_REVISED')


class StoreTests(unittest.TestCase):
    def test_no_legacy_load_and_separate_cohorts(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = Path(root) / 'fast_paper.sqlite3'
            legacy.write_bytes(b'legacy-not-a-database')
            first = EvidenceStore(root, 'trial-A')
            second = EvidenceStore(root, 'trial-B')
            row = points()[0]
            first.append(row)
            first.append(row)
            self.assertEqual(len(first.recent('upbit', 'KRW-TEST', row['observed_ms'])), 1)
            self.assertEqual(second.recent('upbit', 'KRW-TEST', row['observed_ms']), [])
            self.assertEqual(legacy.read_bytes(), b'legacy-not-a-database')
            edited = deepcopy(row)
            edited['current']['rsi'] += 1
            with self.assertRaises(ValueError):
                first.append(edited)
            first.close()
            first = EvidenceStore(root, 'trial-A')
            self.assertEqual(len(first.recent('upbit', 'KRW-TEST', row['observed_ms'])), 1)
            first.close()
            second.close()
            for invalid in ('../old', '/tmp', '', 'a/b'):
                with self.assertRaises(ValueError):
                    state_root(root, invalid)


if __name__ == '__main__':
    unittest.main()
