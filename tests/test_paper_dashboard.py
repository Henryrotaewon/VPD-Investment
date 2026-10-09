import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi1.fast_paper import Paper
from magi2 import paper_dashboard as dashboard, server_runner as server
from magi2.paper_performance import timestamp
from magi2.telegram_ui import main_keyboard, clean_markup, parse_command

T = timestamp('2026-10-09T19:52:23+09:00')


class DashboardTests(unittest.TestCase):
    def test_missing_accounts_read_only_and_all_routes_authorized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram') as send, \
             patch.object(server,'telegram_api'),patch.object(server,'start_engine') as trade:
            for section in dashboard.SECTIONS:
                server.handle_command('paper_'+section,'7','7')
                for key in dashboard.NAMES:
                    server.handle_callback(dict(id='1',data=f'paper:{section}:{key}:0',message=dict(chat=dict(id='7'))))
                    self.assertNotIn('명령을 처리하지 못했습니다',send.call_args.args[0])
            calls=send.call_count
            for data,chat in [('paper:status:fast:0','8'),('paper:status:bad:0','7'),('paper:status:fast:-1','7'),('paper:bad:vpd:0','7')]:
                server.handle_callback(dict(id='1',data=data,message=dict(chat=dict(id=chat))))
            self.assertEqual(send.call_count,calls)
            self.assertEqual(list(Path(tmp).iterdir()),[])
            trade.assert_not_called()

    def test_fast_restart_capital_history_capture_and_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);p=Paper(root/'fast-observe/paper-v1.sqlite3',T-1000)
            p.s.update(cash=2900000.,realized=-100000.,closed=5,winning=1)
            p.pause_entries(T-1);p.resume_entries(T,'test-review')
            t=T+1000;p.signal('KRW-NEW',t,100,95,{})
            p.on_trade('KRW-NEW',t+1,100,1,t+1)
            p.on_book('KRW-NEW',t+2,dict(bp=100,ap=100.01,ts=t+2,stamp=t+2,bids=[(100,100000)],asks=[(100.01,100000)]))
            before=copy.deepcopy(p.s);rows=p.db.execute('SELECT * FROM fills').fetchall()
            text,_=dashboard.view(root,t+3,'results','fast')
            self.assertIn('2,900,000원',text);self.assertIn('청산 0건',text)
            text,_=dashboard.view(root,t+3,'status','fast')
            self.assertIn('KRW-NEW',text);self.assertIn('포착 10/09 19:52',text)
            self.assertIn('목표 고정 없음',text);self.assertIn('최근 매매',text)
            self.assertEqual(p.s,before);self.assertEqual(rows,p.db.execute('SELECT * FROM fills').fetchall())
            p.close(t+4)

    def test_other_live_ledger_schemas_and_bear_target(self):
        from magi2.hourly_indicator import HourlyLedger
        from magi1.bollinger_paper import BollingerPaper
        from magi2.fast_models.bear import BearPaper
        from magi2.fast_models.derivatives import Derivatives
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            indicator=HourlyLedger(root,T)
            bollinger=BollingerPaper(root/'bollinger-paper/v1.sqlite3',T)
            bear=BearPaper(root/'fast-models-v1/bear.sqlite3',T)
            derivatives=Derivatives(root/'fast-models-v1/derivatives.sqlite3',T)
            try:
                for key in ('indicator','bollinger','bear','derivatives'):
                    text,_=dashboard.view(root,T+1,'results',key)
                    self.assertIn('3,000,000원',text,key)
                    self.assertIn('청산 0건',text,key)
                    text,_=dashboard.view(root,T+1,'status',key)
                    self.assertIn('보유 없음',text,key)
                    self.assertNotIn('확인 대기',text,key)
                self.assertIn('가격 기준',dashboard.target('bear',{}))
                self.assertIn('가격 하락 3%',dashboard.target('derivatives',{'kind':'SHORT'}))
                self.assertIn('순익 +0.5%',dashboard.target('derivatives',{'kind':'BASIS'}))
            finally:
                for account in (indicator,bollinger,bear,derivatives):account.db.close()

    def test_vpd_legacy_capture_date_not_fabricated_and_target_from_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            state={'positions':{'X':dict(status='OPEN',first_selected_date='2026-10-01',entry_at='2026-10-01T07:31:00+09:00',target_profit_pct=9,stop_loss_pct=-4)}}
            (root/'paper_state.json').write_text(json.dumps(state))
            text,_=dashboard.view(root,T,'status','vpd')
            self.assertIn('포착 2026-10-01 (일자)',text);self.assertIn('목표 +9%',text)
            self.assertIn('07:30 KST',text)

    def test_top_menu_and_refresh_removed_from_legacy_send(self):
        labels=[b['text'] for row in main_keyboard()['keyboard'] for b in row]
        self.assertEqual(labels,['실투자 현황','모의투자현황','시장국면 (MAGI1)','MAGI 안내·상태'])
        self.assertEqual([parse_command(x) for x in labels],['assets','paper','regime','system_info'])
        mark={'inline_keyboard':[[dict(text='새로고침',callback_data='nav:paper')],[dict(text='🔄 다시 조회',callback_data='nav:regime')],[dict(text='다음 ▶',callback_data='nav:paper')]]}
        self.assertEqual(len(clean_markup(mark)['inline_keyboard']),1)
        with patch.object(server,'BOT_TOKEN','test'),patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram_api') as api:
            server.telegram('test',mark)
            self.assertEqual(api.call_args.args[1]['reply_markup'],clean_markup(mark))

    def test_holdings_paginate_and_bound_message_size(self):
        with patch.object(dashboard,'status_data',return_value=([f'보유 {i}' for i in range(10)],[],[])):
            text,mark=dashboard.view('.',T,'status','fast',4)
            self.assertIn('보유 4',text);self.assertNotIn('보유 3\n',text)
            callbacks=[b['callback_data'] for row in mark['inline_keyboard'] for b in row]
            self.assertIn('paper:status:fast:8',callbacks)
            self.assertLess(len(text),1800)
