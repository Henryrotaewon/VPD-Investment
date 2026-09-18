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
