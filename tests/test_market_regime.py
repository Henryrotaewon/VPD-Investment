import math
import unittest
from concurrent.futures import Future
from unittest.mock import Mock, patch

import numpy as np
import requests

from magi2 import market_regime as regime
from magi2 import server_runner as server
from magi2.telegram_ui import main_keyboard, parse_command


NOW = 1789965000  # 2026-09-21, after the daily settlement grace period.


def payload(pair=regime.PAIRS[0], now=NOW):
    end = regime.cutoff(now)
    start = end - regime.HISTORY_DAYS * regime.DAY
    rows = [[t, '1', '1', '1', str(100 + i), '1', '2', 4]
            for i, t in enumerate(range(start, end + regime.DAY, regime.DAY))]
    return {'error': [], 'result': {pair: rows, 'last': end}}


def prices(direction=1, high=False):
    # Alternating shocks: stable positive/negative 90-day drift, adjustable risk.
    returns = np.array([direction * .003 + (-1)**i * .001 for i in range(399)])
    if high:
        returns[-60:] += np.array([(-1)**i * .02 for i in range(60)])
    return 100 * np.exp(np.r_[0., np.cumsum(returns)])


class DataTests(unittest.TestCase):
    def test_forming_candle_ignored_and_closed_dates_align(self):
        data = payload()
        data['result'][regime.PAIRS[0]][-1][4] = 'nan'
        result = regime.completed_prices(data, regime.PAIRS[0], NOW)
        self.assertEqual(len(result), 400)
        self.assertEqual(result[-1], 499)

    def test_stale_missing_duplicate_future_and_invalid_rejected(self):
        mutations = {
            'stale': lambda rows: rows.pop(-2),
            'missing': lambda rows: rows.pop(100),
            'duplicate': lambda rows: rows.append(rows[100]),
            'future': lambda rows: rows.append([regime.cutoff(NOW) + 2*regime.DAY, 1, 1, 1, 1]),
            'bad_timestamp': lambda rows: rows[100].__setitem__(0, rows[100][0] + .5),
            'nan': lambda rows: rows[100].__setitem__(4, 'nan'),
            'zero': lambda rows: rows[100].__setitem__(4, '0'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = payload()
                mutate(data['result'][regime.PAIRS[0]])
                with self.assertRaises(regime.DataUnavailable):
                    regime.completed_prices(data, regime.PAIRS[0], NOW)

    def test_daily_rollover_grace_uses_previous_settled_day(self):
        midnight = regime.cutoff(NOW)
        self.assertEqual(regime.cutoff(midnight + 179), midnight-regime.DAY)
        self.assertEqual(regime.cutoff(midnight + 180), midnight)

    def test_public_gets_share_cutoff_with_bounded_timeouts(self):
        def get(url, params, timeout):
            self.assertEqual(url, regime.URL)
            self.assertEqual(timeout, (3, 5))
            self.assertEqual(params['interval'], 1440)
            return Mock(json=lambda: payload(params['pair']))
        mock = Mock(side_effect=get)
        result = regime.fetch_regime(NOW, mock)
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(result.close_time, regime.cutoff(NOW))

    def test_http_and_schema_errors_withhold_without_cached_result(self):
        for error in (requests.Timeout(), ValueError('secret internal text'),
                      regime.DataUnavailable('최신 완료 일봉이 없습니다.')):
            with patch.object(regime, 'fetch_regime', side_effect=error):
                text = regime.view()
            self.assertIn('판단 보류', text)
            self.assertNotIn('secret', text)


class ClassificationTests(unittest.TestCase):
    def test_all_four_regimes(self):
        for direction, prefix in ((1, 'UP'), (-1, 'DOWN')):
            for high in (False, True):
                with self.subTest(direction=direction, high=high):
                    p = prices(direction, high)
                    result = regime.classify(p, p, NOW)
                    self.assertEqual(result.state, prefix + ('_HIGH' if high else '_LOW'))

    def test_threshold_excludes_latest_volatility_and_matches_reference(self):
        p = prices(high=True)
        returns = np.diff(np.log(p))
        previous = [np.std(returns[j-30:j], ddof=1)*math.sqrt(365)
                    for j in range(147, 399)]
        result = regime.classify(p, p, NOW)
        self.assertAlmostEqual(result.threshold, float(np.quantile(previous, .65)))
        changed = p.copy()
        changed[-1] *= 1.25
        bumped = regime.classify(changed, changed, NOW)
        self.assertEqual(bumped.threshold, result.threshold)
        self.assertGreater(bumped.volatility, result.volatility)

    def test_one_day_transition_waits_then_two_days_confirms(self):
        p = prices()
        p[-1] *= .5
        result = regime.classify(p, p, NOW)
        self.assertEqual(result.state, 'UP_LOW')
        self.assertEqual(result.observed_state, 'DOWN_HIGH')
        self.assertIn('2일 확인 대기', regime.render(result))
        p[-2] *= .5
        result = regime.classify(p, p, NOW)
        self.assertEqual(result.state, 'DOWN_HIGH')

    def test_disagreement_is_explicit_and_view_has_no_allocation(self):
        result = regime.classify(prices(1), prices(-1), NOW)
        text = regime.render(result)
        self.assertIn('추세가 엇갈림', text)
        self.assertIn('일봉 마감', text)
        self.assertIn('Kraken BTC·ETH/USD', text)
        self.assertLess(len(text), 400)
        for word in ('매수', '매도', '비중', '추천', '확률'):
            self.assertNotIn(word, text)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(server, 'REGIME_JOB', None),
                        patch.object(server, 'REGIME_EXECUTOR'),
                        patch.object(server, 'telegram'),
                        patch.object(server, 'telegram_api'),
                        patch.object(server, 'start_engine'),
                        patch.object(server, 'ALLOWED_CHAT_ID', '7')]
        results = [p.start() for p in self.patches]
        self.executor, self.send, self.api, self.engine = results[1:5]
        self.future = Future()
        self.executor.submit.return_value = self.future
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_button_and_slash_start_one_background_job_without_trading(self):
        labels = [x['text'] for row in main_keyboard()['keyboard'] for x in row]
        self.assertIn('🧭 시장 국면', labels)
        for command in ('🧭 시장 국면', '/regime', '현재 국면'):
            self.assertEqual(parse_command(command), 'regime')
            server.handle_command(command, '7', '7')
        self.executor.submit.assert_called_once_with(server.regime_view)
        self.engine.assert_not_called()
        server.finish_regime_job()
        self.assertIs(server.REGIME_JOB, self.future)
        self.future.set_result('국면 결과')
        server.finish_regime_job()
        self.send.assert_called_with('국면 결과', regime.keyboard())
        self.assertIsNone(server.REGIME_JOB)
        count = self.send.call_count
        server.finish_regime_job()
        self.assertEqual(self.send.call_count, count)
        server.handle_command('/regime', '7', '7')
        self.assertEqual(self.executor.submit.call_count, 2)

    def test_callback_authorization_and_query_route(self):
        callback = {'id': '1', 'data': 'nav:regime', 'from': {'id': '7'},
                    'message': {'chat': {'id': '8'}}}
        server.handle_callback(callback)
        self.executor.submit.assert_not_called()
        callback['message']['chat']['id'] = '7'
        server.handle_callback(callback)
        self.executor.submit.assert_called_once()
        self.engine.assert_not_called()

    def test_unexpected_worker_failure_clears_job_and_shows_hold(self):
        server.start_regime_job()
        self.future.set_exception(RuntimeError('private diagnostic'))
        server.finish_regime_job()
        self.assertIsNone(server.REGIME_JOB)
        self.assertIn('판단 보류', self.send.call_args.args[0])
        self.assertNotIn('private', self.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
