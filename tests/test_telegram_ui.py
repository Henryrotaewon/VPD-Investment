import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from magi2.telegram_ui import (parse_command, Confirmations, main_keyboard, help_text,
                              COMMANDS, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, role_keyboard)
from magi2 import server_runner as server


class MenuTests(unittest.TestCase):
    def test_aliases_slashes_korean_and_legacy(self):
        for text, expected in [('help','help'),('/help','help'),('도움말','help'),
            ('/help@mybot','help'),('magi2 report','report'),('보고서','report'),
            ('📊 자산보고','report'),('magi1 morning scan','morning_scan'),
            ('morning scan','morning_scan'),('/start','menu')]:
            self.assertEqual(parse_command(text,'mybot'),expected,text)
        self.assertIsNone(parse_command('/morning@otherbot','mybot'))
        self.assertIsNone(parse_command('/live'))

    def test_all_menu_buttons_and_help_commands_resolve(self):
        for row in main_keyboard()['keyboard']:
            for button in row: self.assertIsNotNone(parse_command(button['text']))
        for command, description in COMMANDS:
            self.assertEqual(parse_command('/'+command),command)
            self.assertIn('/'+command,help_text())
            self.assertLessEqual(len(description),256)

    def test_profile_limits_and_role_links_are_query_only(self):
        self.assertLessEqual(len(BOT_DESCRIPTION),512)
        self.assertLessEqual(len(BOT_SHORT_DESCRIPTION),120)
        for row in role_keyboard()['inline_keyboard']:
            for button in row:
                command=button['callback_data'].split(':',1)[1]
                self.assertIsNotNone(parse_command(command))
                self.assertNotIn(command,('morning','refill'))

    def test_confirmation_expiry_actor_and_replay(self):
        now=[1]
        confirmations=Confirmations(lambda:now[0])
        menu=confirmations.issue('morning','7','7')
        token=menu['inline_keyboard'][0][0]['callback_data'].split(':')[1]
        self.assertIsNone(confirmations.consume(token,'7','8'))
        self.assertEqual(confirmations.consume(token,'7','7'),'morning')
        self.assertIsNone(confirmations.consume(token,'7','7'))
        token=confirmations.issue('refill','7','7')['inline_keyboard'][0][0]['callback_data'].split(':')[1]
        now[0]=62
        self.assertIsNone(confirmations.consume(token,'7','7'))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.patches=[patch.object(server,'ALLOWED_CHAT_ID','7'),
            patch.object(server,'ALLOWED_USER_IDS',set()),
            patch.object(server,'CONFIRMATIONS',Confirmations()),
            patch.object(server,'ENGINE_JOB',None),
            patch.object(server,'telegram'),patch.object(server,'telegram_api',return_value={}),
            patch.object(server,'start_engine',return_value=True)]
        mocks=[p.start() for p in self.patches]
        self.send,self.api,self.start=mocks[-3:]
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def callback(self,data,chat='7',user='7'):
        return {'id':'callback1','data':data,'from':{'id':user},
                'message':{'message_id':123,'chat':{'id':chat}}}

    def test_confirm_once_no_immediate_execution(self):
        server.handle_command('morning','7','7')
        self.start.assert_not_called()
        markup=self.send.call_args.args[1]
        data=markup['inline_keyboard'][0][0]['callback_data']
        server.handle_callback(self.callback(data))
        self.start.assert_called_once_with('morning')
        server.handle_callback(self.callback(data))
        self.start.assert_called_once()
        self.assertEqual(self.api.call_args_list[0].args[0],'answerCallbackQuery')

    def test_unauthorized_callback_cannot_run_or_consume(self):
        server.handle_command('refill','7','7')
        data=self.send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
        server.handle_callback(self.callback(data,chat='8'))
        server.handle_callback(self.callback(data,user='8'))
        self.start.assert_not_called()
        server.handle_callback(self.callback(data))
        self.start.assert_called_once_with('refill')

    def test_cancel_and_forged_callback_do_not_execute(self):
        server.handle_command('/refill','7','7')
        data=self.send.call_args.args[1]['inline_keyboard'][0][1]['callback_data']
        server.handle_callback(self.callback(data))
        server.handle_callback(self.callback(data.replace('cancel:','confirm:')))
        server.handle_callback(self.callback('nav:morning'))
        self.start.assert_not_called()

    def test_poll_authorization_duplicates_and_callback_dispatch(self):
        updates=[{'update_id':1,'message':{'chat':{'id':'8'},'text':'morning'}},
                 {'update_id':2,'message':{'chat':{'id':'7'},'from':{'id':'7'},'text':'/report'}},
                 {'update_id':2,'message':{'chat':{'id':'7'},'text':'/report'}},
                 {'update_id':3,'callback_query':self.callback('nav:help')}]
        with patch.object(server,'get_updates',return_value=updates):
            self.assertEqual(server.poll_updates(1),4)
        self.start.assert_called_once_with('report')
        self.assertTrue(any(c.args[0]=='answerCallbackQuery' for c in self.api.call_args_list))

    def test_registered_commands_are_scoped_and_localized(self):
        self.api.return_value={'username':'mybot'}
        with patch.object(server,'BOT_USERNAME',''):
            server.setup_telegram_menu()
            self.assertEqual(server.BOT_USERNAME,'mybot')
        calls=[c for c in self.api.call_args_list if c.args[0]=='setMyCommands']
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0].args[1]['scope'],{'type':'chat','chat_id':'7'})

    def test_branding_failure_does_not_block_command_registration(self):
        def reply(method,payload):
            if method=='getMe':return {'username':'mybot'}
            if method=='setMyDescription':raise RuntimeError('unavailable')
            return True
        self.api.side_effect=reply
        server.setup_telegram_menu()
        self.assertTrue(any(c.args[0]=='setMyCommands' for c in self.api.call_args_list))

    def test_keyboard_migration_retries_failure_and_persists_success(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server,'STATE_DIR',Path(directory)):
            self.api.side_effect=RuntimeError('network')
            with self.assertRaises(RuntimeError): server.refresh_telegram_keyboard()
            self.assertFalse((Path(directory)/'telegram_keyboard.json').exists())
            self.api.side_effect=None
            server.refresh_telegram_keyboard()
            payload=self.api.call_args.args[1]
            self.assertEqual(payload['reply_markup'],main_keyboard())
            calls=self.api.call_count
            server.refresh_telegram_keyboard()
            self.assertEqual(self.api.call_count,calls)
        self.start.assert_not_called()

    def test_queries_never_invoke_trading(self):
        for command in ('help','menu','about','status','assets','magi3','signals','strategies'):
            server.handle_command(command,'7','7')
        self.start.assert_not_called()

    def test_status_selection_is_read_only_and_routes_all_three(self):
        server.handle_command('status','7','7')
        buttons=self.send.call_args.args[1]['inline_keyboard']
        self.assertEqual(len(buttons),3)
        with patch.object(server,'fetch_intelligence',return_value=[]), patch.object(server,'execution_view',return_value='MAGI3') as read:
            for row in buttons:
                server.handle_callback(self.callback(row[0]['callback_data']))
            read.assert_called_once_with('magi3')
        self.start.assert_not_called()
        labels=[b['text'] for row in main_keyboard()['keyboard'] for b in row]
        self.assertIn('📊 VPD 모의투자',labels)
        self.assertIn('💼 실계좌 자산',labels)
        self.assertNotIn('⚙️ 실행 상태',labels)

    def test_vpd_submenu_routes_actions_through_confirmation(self):
        server.handle_command('📊 VPD 모의투자','7','7')
        self.start.assert_not_called()
        menu=self.send.call_args.args[1]['inline_keyboard']
        self.assertEqual(menu[0][0]['callback_data'],'nav:report')
        for button,action in zip(menu[1],('morning','refill')):
            server.handle_callback(self.callback(button['callback_data']))
            self.start.assert_not_called()
            confirm=self.send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
            server.handle_callback(self.callback(confirm))
            self.start.assert_called_once_with(action)
            self.start.reset_mock()
        server.handle_callback(self.callback('nav:refill',user='8'))
        self.start.assert_not_called()
        self.assertIn('실행 권한',self.send.call_args.args[0])
        labels=[b['text'] for row in main_keyboard()['keyboard'] for b in row]
        self.assertNotIn('🔄 PAPER 리밸런싱',labels)
        self.assertNotIn('♻️ PAPER 빈자리 채우기',labels)
        self.assertEqual(parse_command('🔄 PAPER 리밸런싱'),'morning')
        self.assertEqual(parse_command('♻️ PAPER 빈자리 채우기'),'refill')
        server.handle_callback(self.callback(menu[-1][0]['callback_data']))
        self.assertEqual(self.send.call_args.args[1],main_keyboard())

    def test_group_mutations_need_named_operator(self):
        with patch.object(server,'ALLOWED_CHAT_ID','-100'):
            self.assertFalse(server.may_execute('-100','7'))
            with patch.object(server,'ALLOWED_USER_IDS',{'7'}):
                self.assertTrue(server.may_execute('-100','7'))
                self.assertFalse(server.may_execute('-100','8'))


class WorkerTests(unittest.TestCase):
    def test_active_job_rejects_parallel_work_and_completed_job_is_collected(self):
        job=Mock()
        with patch.object(server,'ENGINE_JOB',job), patch.object(server,'ENGINE_MODE','monitor'), \
             patch.object(server,'EXECUTOR') as executor:
            job.done.return_value=False
            self.assertFalse(server.start_engine('refill'))
            executor.submit.assert_not_called()
            job.done.return_value=True
            self.assertTrue(server.start_engine('report'))
            job.result.assert_called_once()
            executor.submit.assert_called_once_with(server.run_engine,'report')

    def test_get_updates_requests_callback_updates(self):
        response=Mock()
        response.json.return_value={'ok':True,'result':[]}
        with patch.object(server.TELEGRAM_HTTP,'get',return_value=response) as get:
            server.get_updates(4)
        self.assertIn('callback_query',json.loads(get.call_args.kwargs['params']['allowed_updates']))
        self.assertEqual(get.call_args.kwargs['params']['timeout'],25)
        self.assertGreater(get.call_args.kwargs['timeout'][1],25)

    def test_polling_wait_respects_schedules_and_busy_engine(self):
        with patch.object(server.time,'monotonic',return_value=100):
            self.assertEqual(server.poll_timeout(400,400),25)
            self.assertEqual(server.poll_timeout(103,400),3)
            self.assertEqual(server.poll_timeout(400,400,True),1)
            self.assertEqual(server.poll_timeout(99,400),1)

    def test_startup_drain_is_nonblocking(self):
        with patch.object(server,'get_updates',side_effect=[[{'update_id':7}],[]]) as get:
            self.assertEqual(server.discard_pending_updates(),8)
        self.assertEqual(get.call_args_list[0].kwargs['timeout'],0)
        self.assertEqual(get.call_args_list[1].kwargs['timeout'],0)

if __name__=='__main__': unittest.main()


def test_rescan_command_and_button_are_available():
    from magi2.telegram_ui import parse_command, vpd_keyboard
    assert parse_command('/rescan') == 'rescan'
    assert parse_command('VPD 재스캔') == 'rescan'
    callbacks = [b['callback_data'] for row in vpd_keyboard()['inline_keyboard'] for b in row]
    assert 'nav:rescan' in callbacks
