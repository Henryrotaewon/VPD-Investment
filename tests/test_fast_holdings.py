import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from magi2.fast_target_paper import TargetLedger
from magi2.fast_paper_report import positions_page, position_lines, unit_price, clock
from test_fast_tick import START, RULES, book, tape


class HoldingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.l=TargetLedger(Path(self.tmp.name)/'paper.db',START)
    def tearDown(self):
        self.l.close();self.tmp.cleanup()
    def buy(self,ident='x',venue='upbit',ts=START):
        self.l.offer(ident,venue,'TEST-'+ident,ts,ts)
        self.l.enter_market(ident,book(ts+300,99,100,100000),RULES,ts+300)
        return self.l.get(ident)
    def test_actual_buy_amount_unit_price_and_current_return(self):
        t=self.buy();self.l.mark('upbit',{'TEST-x':(110.055,111)},START+1000)
        text,_=positions_page(self.l,START+1000)
        self.assertIn('TEST-x',text);self.assertIn('포착일시 '+clock(START),text)
        self.assertIn('매수가 100.05원',text)
        self.assertIn('현재가 110.055원',text);self.assertIn('현재 순손익',text);self.assertNotIn('+10.00%',text)
        self.assertIn('매수원금 합계 200,000원',text)
        # The display must not execute orders or alter capital/history.
        self.assertEqual(self.l.get('x')['order'],t['order'])
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],1)
    def test_pending_does_not_fabricate_buy_or_zero_return(self):
        self.l.offer('waiting','upbit','KRW-WAIT',START,START)
        t=self.l.get('waiting');text='\n'.join(position_lines(t,self.l.account('upbit'),START))
        self.assertIn('KRW-WAIT',text);self.assertIn('포착일시',text)
        self.assertIn('상태: 매수 체결 대기 중',text)
        for wrong in ['매수금액','단가','수익률','0.00%']:self.assertNotIn(wrong,text)
        self.l.advance('upbit',START+10001)
        self.assertNotIn('KRW-WAIT',positions_page(self.l,START+10001)[0])
    def test_stale_mark_withholds_return_foreign_currency_and_partial_exit_status(self):
        self.l.fund('binance',1400,'test',START);t=self.buy(venue='binance')
        fresh='\n'.join(position_lines(t,self.l.account('binance'),START+301))
        self.assertIn('매수가 100.05 USDT',fresh);self.assertIn('현재 순손익',fresh)
        stale='\n'.join(position_lines(t,self.l.account('binance'),START+15301))
        self.assertIn('순손익 확인 대기',stale);self.assertNotIn('현재 수익률 -',stale)
        self.assertEqual(unit_price(.00000123,'USDT'),'0.00000123 USDT')
        self.l.liquidate_all(START+1000)
        self.assertIn('시장가 청산 대기 중',positions_page(self.l,START+1000)[0])
    def test_current_positions_precede_closed_and_all_twenty_are_pageable(self):
        self.buy('old');self.l.limit_step('old',tape(START+700),book(START+700,113,114,100000),RULES,START+700)
        for venue in ['upbit','bithumb','binance','kraken']:
            self.l.fund(venue,1400,'test',START)
            for i in range(5):self.l.offer(f'{venue}{i}',venue,f'TEST-{venue}{i}',START+1000,START+1000)
        first,buttons=positions_page(self.l,START+1000)
        second,_=positions_page(self.l,START+1000,10)
        last,_=positions_page(self.l,START+1000,20)
        self.assertEqual(first.count('매수 체결 대기 중'),10)
        self.assertEqual(second.count('매수 체결 대기 중'),10)
        self.assertNotIn('TEST-old',first+second+last)
        self.assertIn('매수 체결 대기 중',last);self.assertIn('fast_results:10',str(buttons))
        self.assertIn('2/2페이지',positions_page(self.l,START+1000,9999)[0])
    def test_pagination_callback_is_read_only_and_chat_scoped(self):
        from magi2 import server_runner as server
        self.buy();paper=Mock();paper.ledger=self.l
        with patch.object(server,'FAST_PAPER',paper),patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram') as send,patch.object(server,'telegram_api'):
            cb={'id':'p','data':'fast_results:0','from':{'id':'7'},'message':{'chat':{'id':'7'}}}
            server.handle_callback(cb)
            self.assertIn('매수원금',send.call_args.args[0]);paper.clear.assert_not_called()
            send.reset_mock();cb['message']['chat']['id']='8';server.handle_callback(cb);send.assert_not_called()

    def test_clear_requires_confirmation_and_rejects_cancel_expiry_replay_other_actor(self):
        from magi2 import server_runner as server
        from magi2.telegram_ui import Confirmations
        ts=[100.]; confirmations=Confirmations(lambda:ts[0])
        paper=Mock();paper.clear.return_value=dict(positions=1,canceled_entries=0)
        def cb(data,user='7'):
            return {'id':'c','data':data,'from':{'id':user},'message':{'message_id':1,'chat':{'id':'7'}}}
        with patch.object(server,'FAST_PAPER',paper),patch.object(server,'CONFIRMATIONS',confirmations),patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'ALLOWED_USER_IDS',{'7','8'}),patch.object(server,'telegram') as send,patch.object(server,'telegram_api'),patch.object(server,'start_engine') as engine:
            # Every entry path, including old buttons, stops at confirmation.
            for command in ['/fast_clear','FAST 정리','🧹 FAST 정리']:
                server.handle_command(command,'7','7');paper.clear.assert_not_called()
            server.handle_callback(cb('nav:fast_clear'))
            markup=send.call_args.args[1];yes,no=markup['inline_keyboard'][0]
            self.assertEqual(yes['text'],'✅ 확인 · 일괄정리 및 포착정지')
            server.handle_callback(cb(yes['callback_data'],'8'));paper.clear.assert_not_called()
            server.handle_callback(cb(no['callback_data']))
            server.handle_callback(cb(yes['callback_data']));paper.clear.assert_not_called()
            server.handle_command('/fast_clear','7','7')
            expired=send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
            ts[0]+=61;server.handle_callback(cb(expired));paper.clear.assert_not_called()
            server.handle_command('/fast_clear','7','7')
            confirmed=send.call_args.args[1]['inline_keyboard'][0][0]['callback_data']
            server.handle_callback(cb(confirmed));server.handle_callback(cb(confirmed))
            paper.clear.assert_called_once();engine.assert_not_called()

if __name__=='__main__':unittest.main()

