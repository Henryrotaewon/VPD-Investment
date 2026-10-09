from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi2 import server_runner as server
from magi2.telegram_ui import main_keyboard, paper_keyboard, parse_command, LEGACY_PAPER_BUTTONS


class HierarchyTests(unittest.TestCase):
    def test_bottom_keyboard_transitions_and_old_buttons_never_trade(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'telegram') as send,patch.object(server,'start_engine') as trade:
            server.handle_command('모의투자현황','7','7')
            self.assertEqual(send.call_args.args[1],paper_keyboard())
            for row in paper_keyboard()['keyboard'][:3]:
                label=row[0]['text']
                self.assertIsNotNone(parse_command(label))
                server.handle_command(label,'7','7')
                self.assertIn(label,send.call_args.args[0])
                self.assertIn('inline_keyboard',send.call_args.args[1])
            server.handle_command('↩️ 메인 메뉴','7','7')
            self.assertEqual(send.call_args.args[1],main_keyboard())
            for label in LEGACY_PAPER_BUTTONS:
                send.reset_mock()
                server.handle_command(label,'7','7')
                self.assertEqual(send.call_args_list[0].args[1],paper_keyboard())
                self.assertEqual(send.call_count,2)
            trade.assert_not_called()

    def test_clear_then_replace_and_retry_if_second_send_fails(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram_api') as api:
            api.side_effect=[{},RuntimeError('send failed')]
            with self.assertRaises(RuntimeError):server.refresh_telegram_keyboard()
            self.assertEqual(api.call_args_list[0].args[1]['reply_markup'],{'remove_keyboard':True})
            self.assertFalse((Path(tmp)/'telegram_keyboard.json').exists())
            api.reset_mock();api.side_effect=None
            server.refresh_telegram_keyboard()
            self.assertEqual(api.call_args_list[0].args[1]['reply_markup'],{'remove_keyboard':True})
            self.assertEqual(api.call_args_list[1].args[1]['reply_markup'],main_keyboard())
            record=json.loads((Path(tmp)/'telegram_keyboard.json').read_text())
            self.assertEqual(record['buttons'],[r[0]['text'] for r in main_keyboard()['keyboard']])
            server.refresh_telegram_keyboard()
            self.assertEqual(api.call_count,2)

    def test_merged_roles_and_status_coalesce_without_blocking(self):
        future=Future()
        with patch.object(server,'SYSTEM_JOB',None),patch.object(server,'SYSTEM_EXECUTOR') as worker, \
             patch.object(server,'telegram') as send,patch.object(server,'start_engine') as trade:
            worker.submit.return_value=future
            for command in ('about','status','system_info'):
                server.handle_command(command,'7','7')
                self.assertEqual(send.call_args.args[1],main_keyboard())
            worker.submit.assert_called_once()
            server.finish_system_job()
            self.assertIs(server.SYSTEM_JOB,future)
            future.set_result('MAGI1 / MAGI2 / MAGI3 상태')
            server.finish_system_job()
            self.assertIsNone(server.SYSTEM_JOB)
            self.assertIn('MAGI1 / MAGI2 / MAGI3',send.call_args.args[0])
            self.assertIn('nav:status3',str(send.call_args.args[1]))
            trade.assert_not_called()

    def test_combined_view_reports_unavailable_and_real_paper_state(self):
        with patch.object(server,'fetch_intelligence',side_effect=ValueError('stale')), \
             patch.object(server,'execution_view',return_value='MAGI3 연결 확인 대기'), \
             patch.dict(server.os.environ,{'MAGI1_INTELLIGENCE_PATH':''}):
            message=server.system_overview('VPD 스캔 중')
            self.assertIn('시장 관측',message)
            self.assertIn('관측 인터페이스 확인 대기',message)
            self.assertIn('PAPER 작업 VPD 스캔 중',message)
            self.assertIn('07:30',message)
            self.assertIn('MAGI3 연결 확인 대기',message)

    def test_unauthorized_callback_cannot_trigger_overview_or_keyboard(self):
        with patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram_api'), \
             patch.object(server,'start_system_job') as start,patch.object(server,'telegram') as send:
            server.handle_callback(dict(id='x',data='nav:system_info',message=dict(chat=dict(id='8'))))
            start.assert_not_called();send.assert_not_called()
