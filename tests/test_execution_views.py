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
    def test_time_order_limit_and_score_meaning(self):
        from magi2.telegram_ui import observation_text
        rows=[{'event_ts_ms':1000*i,'asset':'BTC','strategy_tag':'FAST','direction':'BUY',
               'heuristic_score':100,'evidence':{'venue':'bybit'}} for i in reversed(range(20))]
        text=observation_text(rows)
        self.assertIn('20건 중 최근 15건',text)
        self.assertLess(text.index('09:00:05'),text.index('09:00:19'))
        self.assertNotIn('09:00:04',text)
        self.assertIn('성공확률이 아닙니다',text)
        self.assertIn('하락 둔화도 포함',text)
    def test_whale_unknown_keeps_evidence_separate(self):
        from magi2.telegram_ui import observation_text
        text=observation_text([{'event_ts_ms':1,'asset':'BTC','strategy_tag':'WHALE','direction':'UNKNOWN',
               'heuristic_score':70.5,'evidence':{'amount':1080}}])
        self.assertIn('입출금 방향 미확인',text);self.assertIn('1,080.00 BTC',text)
        self.assertIn('규모점수 70.5',text)
