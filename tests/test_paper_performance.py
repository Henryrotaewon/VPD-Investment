import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from magi1.fast_paper import Paper
from magi2 import paper_engine as engine
from magi2 import server_runner as server
from magi2.hourly_indicator import HourlyLedger
from magi2.paper_performance import (DAY, Mark, Results, Trade, daily_rows,
    fast_results, indicator_results, load_results, record_vpd_mark, timestamp,
    view, vpd_results, win_text)

T = timestamp('2026-09-27T09:00:00+09:00')


class CalculationTests(unittest.TestCase):
    def test_win_denominator_includes_breakeven_not_open_positions(self):
        self.assertEqual(win_text([]), '— (청산 0건)')
        self.assertEqual(win_text([Trade(T, 10), Trade(T, -2), Trade(T, 0)]), '33.3% (1승 1패 1무)')

    def test_nine_am_boundary_and_compounding_not_summing_returns(self):
        r = Results('fast', initial=100, started=T, trades=[Trade(T+DAY-1, 1), Trade(T+DAY, -1)],
                    marks=[Mark(T+DAY-1000, 110), Mark(T+2*DAY-1000, 99)],
                    current=Mark(T+2*DAY-1000, 99))
        rows = daily_rows(r, T+2*DAY-1000)
        self.assertIn('0.0%', rows[0][1])
        self.assertEqual(rows[0][2], '-10.00%')
        self.assertIn('100.0%', rows[1][1])
        self.assertEqual(rows[1][2], '+10.00%')

    def test_missing_or_stale_close_never_becomes_multiday_return(self):
        r = Results('fast', initial=100, started=T,
                    marks=[Mark(T+DAY-1000, 110), Mark(T+2*DAY-1000, 120, False)],
                    current=Mark(T+2*DAY+1000, 130))
        rows = daily_rows(r, T+2*DAY+1000)
        self.assertIn('자료 부족', rows[0][2])
        self.assertIn('자료 부족', rows[1][2])
        r.marks = [Mark(T+DAY-3600000, 110)]
        r.current = Mark(T+DAY+1000, 120)
        self.assertIn('자료 부족', daily_rows(r,T+DAY+1000)[0][2])

    def test_no_trade_day_has_return_and_missing_day_is_visible(self):
        r = Results('indicator', initial=100, started=T,
                    marks=[Mark(T+DAY-1000, 110)], current=Mark(T+DAY+1000, 121))
        day = daily_rows(r, T+DAY+1000)[0]
        self.assertEqual(day[1], '— (청산 0건)')
        self.assertEqual(day[2], '+10.00%')
        r.current = Mark(T+3*DAY+1000, 121)
        rows = daily_rows(r, T+3*DAY+1000)
        self.assertEqual(len(rows), 4)
        self.assertIn('자료 부족', rows[1][2])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def book(self, t, price, size=100000):
        return dict(bp=price, ap=price+.01, ts=t, stamp=t,
                    bids=[(price,size)], asks=[(price+.01,size)])

    def test_fast_partial_exit_over_midnight_and_restart_counts_once(self):
        path = self.root/'fast-observe'/'paper-v1.sqlite3'
        paper = Paper(path,T)
        t = T+1000
        paper.signal('KRW-X',t,100,98,{})
        paper.on_trade('KRW-X',t+1,100,1,t+1)
        paper.on_book('KRW-X',t+2,self.book(t+2,100))
        p = paper.s['positions']['KRW-X']
        q = p['qty']
        paper.request_exit('KRW-X',T+DAY-10000,'TEST')
        paper.on_book('KRW-X',T+DAY-9000,self.book(T+DAY-9000,110,q/2))
        first = fast_results(self.root,T+DAY-8000)
        self.assertEqual(first.trades, [])
        self.assertTrue(first.wins_complete)
        paper.on_book('KRW-X',T+DAY+1000,self.book(T+DAY+1000,95))
        result = fast_results(self.root,T+DAY+1001)
        self.assertEqual(len(result.trades),1)
        self.assertTrue(result.wins_complete)
        expected = paper.db.execute("SELECT SUM(pnl) FROM fills WHERE side='SELL'").fetchone()[0]
        self.assertAlmostEqual(result.trades[0].pnl,expected)
        self.assertEqual(result.trades[0].ts//DAY,(T+DAY)//DAY)
        self.assertGreater(expected,0)
        paper.close(T+DAY+2000)
        reopened = Paper(path,T+DAY+3000)
        self.assertEqual(fast_results(self.root,T+DAY+3001).trades,result.trades)
        reopened.close(T+DAY+4000)

    def test_fast_detects_truncated_fills_instead_of_false_win_rate(self):
        paper = Paper(self.root/'fast-observe'/'paper-v1.sqlite3',T)
        paper.s['closed']=2
        paper.save(T+1)
        self.assertFalse(fast_results(self.root,T+2).wins_complete)
        paper.close(T+3)

    def test_indicator_uses_fill_date_not_delayed_record_time(self):
        ledger = HourlyLedger(self.root,T)
        ledger.event(T+DAY+1000,'SELL','KRW-X',dict(fill_ms=T+DAY-1000,pnl=42))
        ledger.save()
        ledger.mark({},T+DAY-2000)
        result = indicator_results(self.root,T+DAY+2000)
        self.assertEqual(result.trades,[Trade(T+DAY-1000,42)])
        self.assertEqual(len(result.marks),1)
        ledger.db.close()

    def state(self):
        return dict(initial_cash_krw=1000,cash_krw=600,lifetime_realized_pnl_krw=10,
                    cohort_id='OLD',positions={
            'X':dict(market='KRW-X',status='OPEN',entry_at='2026-09-26T10:00:00+09:00',qty=4,cost_krw=400),
            'Y':dict(market='KRW-Y',status='CLOSED',entry_at='2026-09-26T10:00:00+09:00',
                     exit_at='2026-09-27T10:00:00+09:00',pnl_krw=10)})

    def save_vpd(self,s):
        (self.root/'paper_state.json').write_text(json.dumps(s))
        (self.root/'paper_events.jsonl').write_text(json.dumps(dict(type='SELL',market='KRW-Y',coin='Y',
            entry_at='2026-09-26T10:00:00+09:00',ts='2026-09-27T10:00:00+09:00',pnl_krw=10))+'\n')

    def test_vpd_deduplicates_state_and_events_keeps_cohort_history(self):
        s=self.state();self.save_vpd(s)
        path=self.root/'paper_state.json'
        record_vpd_mark(path,s,{'KRW-X':100},.0005,T+DAY-1000)
        s['cohort_id']='NEW';self.save_vpd(s)
        record_vpd_mark(path,s,{'KRW-X':110},.0005,T+DAY+1000)
        r=vpd_results(self.root,T+DAY+2000)
        self.assertEqual(len(r.trades),1)
        self.assertTrue(r.wins_complete)
        self.assertEqual(len(r.marks),2)
        self.assertAlmostEqual(r.current.equity,600+4*110*.9995)
        self.assertIn('+4.00%',daily_rows(r,T+DAY+2000)[0][2])
        self.assertIn('자료 부족',daily_rows(r,T+DAY+2000)[1][2])

    def test_vpd_missing_quote_marks_invalid_and_identity_prevents_old_nav(self):
        s=self.state();self.save_vpd(s)
        path=self.root/'paper_state.json'
        record_vpd_mark(path,s,{},.0005,T+5000000)
        self.assertIsNone(vpd_results(self.root,T+5000001).current.equity)
        record_vpd_mark(path,s,{'KRW-X':100},.0005,T+6000000)
        s['cash_krw']=700;self.save_vpd(s)
        self.assertIsNone(vpd_results(self.root,T+6000001).current)

    def test_vpd_microsecond_exit_difference_and_legacy_missing_entry(self):
        s=self.state();self.save_vpd(s)
        event=dict(type='SELL',market='KRW-Y',coin='Y',entry_at='2026-09-26T10:00:00+09:00',
                   ts='2026-09-27T10:00:00.000025+09:00',pnl_krw=10)
        for legacy in (False, True):
            if legacy:event.pop('entry_at')
            (self.root/'paper_events.jsonl').write_text(json.dumps(event)+'\n')
            r=vpd_results(self.root,T+DAY)
            self.assertEqual(len(r.trades),1)
            self.assertTrue(r.wins_complete)

    def test_missing_ledger_is_read_only_and_detail_is_paginated(self):
        text,buttons=view(self.root,T)
        self.assertIn('VPD',text);self.assertIn('FAST',text);self.assertIn('지표가속',text)
        self.assertEqual(list(self.root.iterdir()),[])
        r=Results('vpd',initial=1000,trades=[Trade(T-15*DAY,1)],current=Mark(T,1100))
        with patch('magi2.paper_performance.load_results',return_value=r):
            text,buttons=view(self.root,T,'vpd',7)
        self.assertTrue(text.startswith('• 누적 승률 100.0%'))
        self.assertIn('수익률 +10.00%',text.splitlines()[0])
        self.assertIn('2/3페이지',text)
        self.assertLess(len(text),4096)

    def test_snapshot_failure_does_not_interrupt_paper_worker(self):
        with patch('magi2.paper_performance.record_vpd_mark',side_effect=sqlite3.OperationalError('locked')):
            engine.record_performance({}, {})


class RoutingTests(unittest.TestCase):
    def test_overview_and_all_detail_buttons_read_only_and_authorized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server,'STATE_DIR',Path(tmp)), \
             patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'ALLOWED_USER_IDS',set()), \
             patch.object(server,'telegram') as send,patch.object(server,'telegram_api'), \
             patch.object(server,'start_engine') as trade:
            server.handle_command('strategies','7','7')
            for key in ('vpd','fast','indicator','bollinger'):
                server.handle_callback(dict(id='1',data=f'performance:{key}:0',
                    message=dict(chat=dict(id='7')),**{'from':dict(id='7')}))
                self.assertTrue(send.call_args.args[0].startswith('• 누적 승률'))
            calls=send.call_count
            for data,user in [('performance:fast:0','8'),('performance:invalid:0','7'),('performance:vpd:-1','7')]:
                server.handle_callback(dict(id='1',data=data,message=dict(chat=dict(id='8' if user=='8' else '7')),**{'from':dict(id=user)}))
            self.assertEqual(send.call_count,calls)
            trade.assert_not_called()


if __name__ == '__main__':
    unittest.main()
