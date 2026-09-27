from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi1.bollinger_paper import BollingerPaper
from magi2 import server_runner as server
from magi2.bollinger_report import view
from magi2.paper_performance import results_keyboard, timestamp
from magi2.telegram_ui import parse_command

T = timestamp('2026-09-28T07:00:00+09:00')


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = BollingerPaper(self.root/'bollinger-paper'/'v1.sqlite3', T)

    def tearDown(self):
        self.p.close(T+1000000)
        self.tmp.cleanup()

    def book(self, t, bid=100, size=100000):
        return dict(bp=bid, ap=bid+.01, ts=t, stamp=t,
                    bids=[(bid,size)], asks=[(bid+.01,size)])

    def buy(self, symbol='KRW-X', t=T+1):
        self.p.signal(symbol, t, 100, 98, {})
        self.p.on_trade(symbol, t+1, 100, 1, t+1)
        self.p.on_book(symbol, t+2, self.book(t+2))

    def test_partial_exit_and_same_day_reentry_are_separate_round_trips(self):
        self.buy()
        qty = self.p.s['positions']['KRW-X']['qty']
        self.p.request_exit('KRW-X', T+100, 'BB_MIDDLE_AND_CCI_NEGATIVE')
        self.p.on_book('KRW-X', T+101, self.book(T+101, 105, qty/2))
        text, _ = view(self.root, T+102)
        self.assertIn('일부 매도·보유 중', text)
        self.assertIn('잔여 수량', text)
        self.assertNotIn('순수익률', text.split('순손익·순수익률')[0])
        self.p.on_book('KRW-X', T+103, self.book(T+103, 106))
        pnl = self.p.s['realized']
        proceeds = self.p.db.execute("SELECT SUM(cash) FROM fills WHERE side='SELL'").fetchone()[0]
        self.buy(t=T+200)
        before = self.p.db.total_changes
        text, _ = view(self.root, T+300)
        self.assertEqual(self.p.db.total_changes, before)
        self.assertIn('매수 거래 2건', text)
        self.assertEqual(text.count('• KRW-X'), 2)
        self.assertEqual(text.count('매도완료'), 1)
        self.assertLess(text.index('보유 중'), text.index('매도완료'))
        self.assertIn(f'실현손익 {pnl:+,.0f}원', text)
        self.assertIn(f'매도수령액 {proceeds:,.0f}원', text)
        self.assertIn(f'순수익률 {pnl/300000*100:+.2f}%', text)
        self.assertIn('매도 2회 평균 · 최종', text)
        self.assertIn('5분 종가가 단기 중심선 하회 + CCI 음수', text)
        self.assertIn('밴드 수축 후 상단 돌파', text)
        self.assertIn('09/28 07:00:00', text)

    def test_snapshot_pages_are_stable_when_new_fills_arrive(self):
        for n in range(7):
            self.buy(f'KRW-X{n}', T+n*100+1)
        text, keys = view(self.root, T+1000)
        next_data = next(b['callback_data'] for row in keys['inline_keyboard'] for b in row if b['text']=='이전 거래 ▶')
        _, anchor, offset = next_data.split(':')
        old_text, _ = view(self.root, T+1000, int(anchor), int(offset))
        self.assertIn('2/2페이지', old_text)
        self.assertIn('• KRW-X0', old_text)
        self.assertNotIn('• KRW-X2', old_text)
        self.p.request_exit('KRW-X0', T+1100, 'PROTECTION')
        self.p.on_book('KRW-X0', T+1101, self.book(T+1101, 97))
        self.buy('KRW-NEW', T+1200)
        same_text, _ = view(self.root, T+1300, int(anchor), int(offset))
        self.assertEqual(old_text, same_text)
        refreshed, _ = view(self.root, T+1300)
        self.assertIn('매수 거래 8건', refreshed)
        self.assertIn('KRW-NEW', refreshed)
        self.assertIn('초기 보호선(신호 포함 최근 6봉 저점) 이탈', refreshed)
        self.assertLess(len(text.encode('utf-16-le'))//2, 4096)
        self.assertLess(len(refreshed.encode('utf-16-le'))//2, 4096)

    def test_pending_is_not_a_fill_and_missing_database_is_not_created(self):
        self.p.signal('KRW-X', T+1, 100, 98, {})
        text, _ = view(self.root, T+2)
        self.assertIn('체결 기록 없음', text)
        self.assertIn('매수 대기 1종목', text)
        self.assertNotIn('• KRW-X', text)
        with tempfile.TemporaryDirectory() as missing:
            text, _ = view(missing, T+2)
            self.assertIn('원장 준비 중', text)
            self.assertEqual(list(Path(missing).iterdir()), [])

    def test_gap_reason_is_korean_and_out_of_range_page_clamps(self):
        self.buy()
        self.p.gap(['KRW-X'], T+100, 'RESTART')
        self.p.on_book('KRW-X', T+101, self.book(T+101))
        text, _ = view(self.root, T+300000, offset=1000000)
        self.assertIn('1/1페이지', text)
        self.assertIn('재시작으로 관측 단절', text)
        self.assertIn('원장 갱신 지연', text)
        self.assertNotIn('DATA_GAP_', text)


class RoutingTests(unittest.TestCase):
    def test_result_button_command_pagination_and_readonly_access(self):
        self.assertIn('nav:bollinger_orders', str(results_keyboard('bollinger')))
        self.assertNotIn('nav:bollinger_orders', str(results_keyboard('fast')))
        self.assertEqual(parse_command('/bollinger_orders'), 'bollinger_orders')
        with tempfile.TemporaryDirectory() as d, patch.object(server,'STATE_DIR',Path(d)), \
             patch.object(server,'ALLOWED_CHAT_ID','7'), patch.object(server,'ALLOWED_USER_IDS',set()), \
             patch.object(server,'telegram') as send, patch.object(server,'telegram_api'), \
             patch.object(server,'start_engine') as trade:
            server.handle_command('/bollinger_orders','7','7')
            for data in ('nav:bollinger_orders', 'bollinger_orders:12:5'):
                server.handle_callback(dict(id='1',data=data,message=dict(chat=dict(id='7')),**{'from':dict(id='7')}))
                self.assertIn('더블볼린저·CCI 매매이력', send.call_args.args[0])
            count = send.call_count
            for data,chat in [('bollinger_orders:1:0','8'), ('bollinger_orders:no:5','7'),
                              ('bollinger_orders:1:-1','7'), ('bollinger_orders:9223372036854775808:0','7'),
                              ('bollinger_orders:1:0:extra','7')]:
                server.handle_callback(dict(id='1',data=data,message=dict(chat=dict(id=chat)),**{'from':dict(id=chat)}))
            self.assertEqual(send.call_count, count)
            self.assertEqual(list(Path(d).iterdir()), [])
            trade.assert_not_called()


if __name__ == '__main__':
    unittest.main()
