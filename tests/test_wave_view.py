import copy
import unittest
from unittest.mock import Mock, patch
from magi1.wave_analysis import build_report
from magi1.tests.test_wave_analysis import dataset, NOW
from magi2.wave_view import render, validate, WaveClient
from magi2 import server_runner as server


class WaveViews(unittest.TestCase):
    def setUp(self):
        self.report = build_report(dataset(), NOW)

    def test_three_views_separate_observation_evidence_and_trade_readiness(self):
        for section in ('overview', 'strength', 'evidence', 'trading'):
            text, menu = render(self.report, section, now=NOW)
            self.assertLess(len(text), 1800)
            self.assertTrue(menu['inline_keyboard'])
        text, _ = render(self.report, 'evidence', now=NOW)
        self.assertIn('표본 부족', text)
        self.assertIn('선별된 사건 목록', text)
        self.assertIn('실제 100.0% / 대조 0.0%', text)
        text, _ = render(self.report, 'trading', now=NOW)
        self.assertIn('검증 전', text)
        self.assertIn('미확인', text)
        self.assertIn('수수료·주문 지연', text)

    def test_missing_stale_pagination_and_no_unmeasured_zero(self):
        self.assertIn('준비', render(None)[0])
        self.assertIn('마지막 분석본', render(self.report, now=NOW+900000)[0])
        text, _ = render(build_report(dataset(controls=(None,)*5), NOW), 'evidence', now=NOW)
        self.assertNotIn('대조 0.0%', text)
        _, menu = render(self.report, 'strength', now=NOW)
        callbacks = [b['callback_data'] for row in menu['inline_keyboard'] for b in row]
        self.assertIn('wave:strength:4', callbacks)
        _, menu = render(self.report, 'strength', offset=9999, now=NOW)
        self.assertTrue(any('wave:strength:0' == b['callback_data'] for row in menu['inline_keyboard'] for b in row))

    def test_transport_contract_rejects_execution_future_and_expired(self):
        self.assertIs(validate(self.report, NOW), self.report)
        for change in ({'execution_eligible': True}, {'mode': 'LIVE'}, {'calibrated_probability': .9},
                       {'generated_ts_ms': NOW+1}, {'expires_ts_ms': NOW}):
            with self.assertRaises(ValueError):
                validate(self.report | change, NOW)

    def test_cached_views_never_fetch_on_command_or_callback_and_auth_is_checked(self):
        client = WaveClient('http://magi1:8081/intelligence', 'x'*32)
        client.report = self.report
        self.assertEqual(client.url, 'http://magi1:8081/wave')
        callback = {'id': 'x', 'data': 'wave:evidence:0', 'message': {'chat': {'id': '7'}}, 'from': {'id': '7'}}
        with patch.object(server, 'ALLOWED_CHAT_ID', '7'), patch.object(server, 'WAVE_CLIENT', client), \
             patch.object(server, 'telegram') as send, patch.object(server, 'telegram_api'), \
             patch.object(server, 'start_engine') as trade, patch('magi2.wave_view.requests.Session') as network:
            server.handle_command('/wave', '7', '7')
            server.handle_callback(callback)
            server.handle_callback(callback | {'data': 'guide:wave'})
            self.assertEqual(send.call_count, 3)
            server.handle_callback(callback | {'message': {'chat': {'id': '8'}}})
            server.handle_callback(callback | {'data': 'wave:evidence:-1'})
            server.handle_callback(callback | {'data': 'wave:evidence:notint'})
            self.assertEqual(send.call_count, 3)
            network.assert_not_called()
            trade.assert_not_called()


if __name__ == '__main__':
    unittest.main()
