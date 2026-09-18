import json
import unittest
from unittest.mock import patch, Mock
from magi2.telegram_ui import (parse_command, Confirmations, main_keyboard, help_text,
                              COMMANDS)
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

    def test_queries_never_invoke_trading(self):
        for command in ('help','menu','status','assets','magi3','signals','strategies'):
            server.handle_command(command,'7','7')
        self.start.assert_not_called()

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
        with patch.object(server.requests,'get',return_value=response) as get:
            server.get_updates(4)
        self.assertIn('callback_query',json.loads(get.call_args.kwargs['params']['allowed_updates']))

if __name__=='__main__': unittest.main()
