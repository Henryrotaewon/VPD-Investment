import tempfile
import unittest
from unittest.mock import patch
from magi3.runtime_store import RuntimeStore
from magi2 import server_runner as server
from magi2.execution_client import orders_page, render_orders
from magi2.telegram_ui import main_keyboard

class ShadowHistoryTests(unittest.TestCase):
    def test_72_hour_boundary_and_stable_pages(self):
        now=1700000000000
        cutoff=now-72*3600000
        with tempfile.TemporaryDirectory() as root:
            store=RuntimeStore(root)
            with store.transaction() as db:
                for ident,ts in [('old',cutoff-1),('boundary',cutoff),('future',now+1)]+[(f'o{i:02}',now-i) for i in range(25)]:
                    db.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)',(ident,'s','p','BUY','upbit','BTC','FILLED','SHADOW',ts))
            first=store.orders_window(now)
            second=store.orders_window(now,first['next_offset'])
            self.assertEqual(first['total'],26)
            self.assertEqual(len(first['orders']),20)
            self.assertEqual(len(second['orders']),6)
            self.assertIsNone(second['next_offset'])
            ids=[o['id'] for o in first['orders']+second['orders']]
            self.assertEqual(len(set(ids)),26)
            self.assertIn('boundary',ids); self.assertNotIn('old',ids);self.assertNotIn('future',ids)
            text=render_orders(first)
            self.assertIn('KST',text);self.assertIn('체결 —',text)
            with patch('magi2.execution_client.fetch',return_value=first):
                text,markup=orders_page(now)
                self.assertEqual(markup['inline_keyboard'][0][0]['callback_data'],f'orders:{now}:20')
            store.close()

    def test_menu_and_history_are_read_only_and_authorized(self):
        with patch.object(server,'telegram') as send,patch.object(server,'start_engine') as trade,patch.object(server,'send_shadow_orders') as history,patch.object(server,'telegram_api'),patch.object(server,'ALLOWED_CHAT_ID','7'):
            server.handle_command('🧪 shadows 모의투자','7','7')
            self.assertEqual(send.call_args.args[1]['inline_keyboard'][0][1]['callback_data'],'nav:orders')
            callback={'id':'x','data':'orders:1800000000000:20','message':{'chat':{'id':'8'}},'from':{'id':'8'}}
            server.handle_callback(callback);history.assert_not_called()
            callback['message']['chat']['id']='7'
            server.handle_callback(callback);history.assert_called_once_with(1800000000000,20)
            trade.assert_not_called()
        labels=[b['text'] for row in main_keyboard()['keyboard'] for b in row]
        self.assertIn('🧪 shadows 모의투자',labels)
        self.assertNotIn('🧪 Shadow 자산',labels);self.assertNotIn('📒 Shadow 원장',labels)

    def test_empty_history_is_not_service_failure(self):
        r={'since_ts_ms':1000,'until_ts_ms':259201000,'total':0,'offset':0,'next_offset':None,'orders':[]}
        with patch('magi2.execution_client.fetch',return_value=r):
            self.assertIn('해당 기간에 모의 매매이력이 없습니다',orders_page()[0])
        with patch('magi2.execution_client.fetch',side_effect=RuntimeError):
            self.assertIn('불러오지 못했습니다',orders_page()[0])
