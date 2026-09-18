import unittest
from unittest.mock import patch
from magi2 import server_runner as server
from magi2.execution_client import view,render_status

class ExecutionViewsTests(unittest.TestCase):
    def test_routes_do_not_start_paper_or_live(self):
        with patch.object(server,'telegram') as send,patch.object(server,'execution_view',return_value='snapshot') as read,patch.object(server,'start_engine') as trade:
            for cmd in ('assets','shadow','orders','execution','magi3'):
                server.handle_command('/'+cmd,'7','7');read.assert_called_with(cmd)
            trade.assert_not_called();send.assert_called_with('snapshot')
    def test_service_failure_not_fake_balance(self):
        with patch('magi2.execution_client.fetch',side_effect=RuntimeError('secret raw exception')):
            text=view('assets');self.assertNotIn('secret',text);self.assertIn('연결하지 못했거나',text)
    def test_stale_status_disclosed(self):
        text=render_status({'mode':'SHADOW','heartbeat_ms':1,'loop_status':'OK','feed_status':'UNAVAILABLE'})
        self.assertIn('응답 지연',text);self.assertIn('실주문 OFF',text)


class ObservationViewTests(unittest.TestCase):
    def test_old_acceleration_is_not_new_fast(self):
        from magi2.telegram_ui import observation_text
        rows=[{'event_ts_ms':1,'asset':'BTC','strategy_tag':'FAST','direction':'BUY','heuristic_score':100,'evidence':{}}]
        text=observation_text(rows,'fast')
        self.assertIn('기존 가속 지표 1건',text);self.assertIn('후보 없음',text)
    def test_separate_fast_and_wave_roles(self):
        from magi2.telegram_ui import observation_text
        rows=[{'event_ts_ms':1000,'asset':'BTC','strategy_tag':'WHALE','direction':'UNKNOWN','heuristic_score':70.5,'evidence':{'amount':1080}},
              {'event_ts_ms':2000,'asset':'ETH','strategy_tag':'FAST','direction':'BUY','heuristic_score':90,
               'evidence':{'fast_rule_version':'fast-rise-v1','venue':'upbit','return_bps':25,'volume_ratio':3,'coverage_ms':9000}}]
        fast=observation_text(rows,'fast');wave=observation_text(rows,'wave')
        self.assertIn('+0.25%',fast);self.assertIn('3.00배',fast);self.assertNotIn('1,080',fast)
        self.assertIn('1,080.00 BTC',wave);self.assertIn('독립 매매 전략이 아닙니다',wave)
        self.assertNotIn('ETH',wave);self.assertNotIn('규모점수',wave)
