import asyncio
import json
import math
from pathlib import Path
from statistics import fmean, pstdev
import tempfile
import unittest
from unittest.mock import patch

from magi1.bollinger_paper import BollingerPaper, FIVE, HISTORY, features, warm_history
from magi1.fast_observe import Observer
from magi1.fast_paper import DAY
from magi2.paper_performance import bollinger_results, view

T = 30*DAY
S = 'KRW-X'


def history(end=T, n=HISTORY):
    rows = []
    for i in range(n):
        close = 100 + ((.8 if i < n-80 else .01) * (-1 if i%2 else 1))
        rows.append([end-(n-i)*FIVE, close, close+.01, close-.01, close, 1000, 100000])
    return rows


def candle(t, c=100.01, value=100000):
    return [t, c, c+.01, c-.01, c, value/c, value]


def book(t, bid=100., ask=100.01, size=100000, stamp=None):
    return dict(bp=bid, ap=ask, bq=size, aq=size, ts=t, stamp=t if stamp is None else stamp,
                bids=[(bid, size)], asks=[(ask, size)])


class MathTests(unittest.TestCase):
    def test_matches_independent_sd_percentile_and_cci_with_current_excluded(self):
        rows = history()
        rows[-1] = candle(rows[-1][0], 100.3, 300000)
        f = features(rows)
        widths = []
        for end in range(60, HISTORY):
            c = [r[4] for r in rows[end-60:end]]
            widths.append(4*pstdev(c)/fmean(c)*100)
        ordered = sorted(widths)
        rank = 287*.2
        expected = ordered[57]*(1-(rank-57)) + ordered[58]*(rank-57)
        self.assertAlmostEqual(f['squeeze_threshold'], expected)
        recent = [r[4] for r in rows[-20:]]
        self.assertAlmostEqual(f['short_upper'], fmean(recent)+2*pstdev(recent))
        typical = [(r[2]+r[3]+r[4])/3 for r in rows[-10:]]
        mean = fmean(typical)
        self.assertAlmostEqual(f['cci'], (typical[-1]-mean)/(.015*fmean(abs(x-mean) for x in typical)))
        self.assertEqual(f['turnover_mean'], 100000)
        self.assertTrue(f['breakout'] and f['expanding'] and f['long_up'] and f['volume_ok'])

    def test_missing_bars_not_backfilled_and_flat_prices_are_finite(self):
        self.assertIsNone(features(history(n=347)))
        rows = history(n=349)
        rows.pop(100)
        self.assertIsNone(features(rows))
        rows = [candle(i*FIVE, 100) for i in range(HISTORY)]
        f = features(rows)
        self.assertEqual(f['cci'], 0)
        self.assertFalse(f['breakout'])
        self.assertTrue(all(math.isfinite(x) for x in f.values()))


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'bollinger-paper'/'v1.sqlite3'
        self.p = BollingerPaper(self.path, T)
        self.p.connected([S], T)

    def tearDown(self):
        self.p.close(T+100*FIVE)
        self.tmp.cleanup()

    def live_bar(self, bucket, price=100.01, value=100000):
        self.p.candle_trade(S, bucket+1, price, value/price, bucket+1, bucket+1)
        self.p.tick(bucket+FIVE+2500)

    def signal(self, t=T+1, low=98):
        self.p.signal(S, t, 100, low, {})
        self.p.on_trade(S, t+1, 100, 1, t+1)
        return t

    def buy(self, t=T+1):
        self.signal(t)
        self.p.on_book(S, t+2, book(t+2))
        return self.p.s['positions'][S]

    def test_history_does_not_arm_or_buy_then_live_squeeze_breakout_buys(self):
        self.p.seed(S, history(), T)
        self.assertFalse(self.p.watch)
        self.assertFalse(self.p.s['pending'])
        self.live_bar(T)
        self.assertEqual(self.p.watch[S], T)
        self.live_bar(T+FIVE, 100.3, 300000)
        self.assertIn(S, self.p.s['pending'])
        decision = T+2*FIVE+2500
        self.assertEqual(self.p.s['pending'][S]['ts'], decision)
        self.p.on_trade(S, decision+1, 100.3, 1, decision+1)
        self.p.on_book(S, decision+2, book(decision+2, 100.29, 100.3))
        self.assertIn(S, self.p.s['positions'])
        self.assertAlmostEqual(self.p.s['positions'][S]['stop'], 99.98)

    def test_no_same_bar_squeeze_entry_and_six_bar_window_boundary(self):
        self.p.seed(S, history(), T)
        self.live_bar(T, 100.3, 300000)
        self.assertFalse(self.p.s['pending'])
        f = dict(features(history()), squeeze=False, breakout=True, expanding=True,
                 long_up=True, cci=150, volume_ok=True)
        for age, expected in [(6, True), (7, False)]:
            with self.subTest(age=age), patch('magi1.bollinger_paper.features', return_value=f):
                self.p.watch[S] = T
                bucket = T+age*FIVE
                self.p.store_rows(S, [candle(bucket)])
                self.p.assess(S, bucket, bucket+FIVE+2500)
                self.assertEqual(S in self.p.s['pending'], expected)
                self.p.cancel(S, bucket+FIVE+3000, 'TEST')

    def test_each_entry_filter_required_but_no_cci_cross_requirement(self):
        base = dict(features(history()), squeeze=False, breakout=True, expanding=True,
                    long_up=True, cci=150, volume_ok=True)
        self.p.store_rows(S, [candle(T+FIVE)])
        for key, bad in [('breakout',False), ('expanding',False), ('long_up',False), ('cci',100), ('volume_ok',False)]:
            self.p.watch[S] = T
            with self.subTest(key=key), patch('magi1.bollinger_paper.features', return_value=dict(base, **{key:bad})):
                self.p.assess(S, T+FIVE, T+2*FIVE+2500)
                self.assertFalse(self.p.s['pending'])
        self.p.watch[S] = T
        with patch('magi1.bollinger_paper.features', return_value=base):
            self.p.assess(S, T+FIVE, T+2*FIVE+2500)
        self.assertIn(S, self.p.s['pending'])

    def test_entry_depth_cap_expiry_and_expected_fill_stop_distance(self):
        t = self.signal()
        self.p.on_book(S, t+2, book(t+2, size=1))
        self.assertEqual(self.p.s['pending'][S]['last_reason'], 'INSUFFICIENT_VISIBLE_DEPTH')
        self.p.on_book(S, t+3, book(t+3, ask=100.3))  # Slippage pushes it beyond +0.3%.
        self.assertEqual(self.p.s['pending'][S]['last_reason'], 'ENTRY_PRICE_CAP')
        self.p.tick(t+10001)
        self.assertFalse(self.p.s['positions'])
        self.assertFalse(self.p.s['pending'])
        t = self.signal(T+20000, low=97)
        self.p.on_book(S, t+2, book(t+2))  # Actual slipped price, not signal close.
        self.assertFalse(self.p.s['positions'])
        reason = self.p.db.execute('SELECT reason FROM signals').fetchone()[0]
        self.assertEqual(reason, 'STOP_DISTANCE_OVER_3_PERCENT')

    def test_no_fast_idle_flow_trail_exit_and_trend_requires_both_conditions(self):
        p = self.buy()
        t = T+4*60_000
        self.p.on_book(S, t, book(t, 99.99, 100))
        self.p.window(S, t, [])
        self.assertIsNone(p['exit'])
        self.assertEqual(p['stop'], 98)
        self.p.store_rows(S, [candle(T)])
        for close, cci in [(99, 1), (101, -1), (101, 250)]:
            f = dict(features(history()), close=close, short_middle=100, cci=cci)
            with patch('magi1.bollinger_paper.features', return_value=f):
                self.p.assess(S, T, T+FIVE+2500)
            self.assertIsNone(p['exit'])
        f = dict(f, close=99, cci=-1)
        with patch('magi1.bollinger_paper.features', return_value=f):
            self.p.assess(S, T, T+FIVE+2500)
        self.assertEqual(p['exit']['reason'], 'BB_MIDDLE_AND_CCI_NEGATIVE')
        self.p.on_book(S, T+FIVE+2501, book(T+FIVE+2501))
        self.assertFalse(self.p.s['positions'])

    def test_stop_next_quote_partial_fills_and_reentry_needs_new_squeeze(self):
        p = self.buy()
        qty = p['qty']
        t = T+100
        self.p.on_trade(S, t, 97, 1, t)
        self.p.on_book(S, t+1, book(t+1, 96, 96.1, size=qty/2))
        self.assertIn(S, self.p.s['positions'])
        self.assertEqual(self.p.s['closed'], 0)
        self.p.watch[S] = T-FIVE
        self.p.on_book(S, t+2, book(t+2, 95, 95.1))
        self.assertEqual(self.p.s['closed'], 1)
        self.assertNotIn(S, self.p.watch)
        self.assertEqual(self.p.s['last_exit_ms'][S], t+2)
        self.assertTrue(self.p.can_signal(S, t+3))  # No blanket daily ban.
        self.p.store_rows(S, [candle(T)])
        f = dict(features(history()), squeeze=True)
        with patch('magi1.bollinger_paper.features', return_value=f):
            self.p.assess(S, T, T+FIVE+2500)
        self.assertNotIn(S, self.p.watch)  # Candle started before exit.
        self.p.store_rows(S, [candle(T+FIVE)])
        with patch('magi1.bollinger_paper.features', return_value=f):
            self.p.assess(S, T+FIVE, T+2*FIVE+2500)
        self.assertEqual(self.p.watch[S], T+FIVE)
        result = bollinger_results(self.root, t+3)
        self.assertTrue(result.wins_complete)
        self.assertEqual(len(result.trades), 1)
        self.assertLess(result.trades[0].pnl, 0)

    def test_exchange_timestamp_ohlc_out_of_order_and_settlement(self):
        self.p.seed(S, history(), T)
        for stamp, received, price, ident in [(T+1000,T+1000,101,2), (T+500,T+1200,99,1), (T+2000,T+2001,100,3)]:
            self.p.candle_trade(S, received, price, 1, stamp, ident)
        self.p.tick(T+FIVE+2000)
        self.assertEqual(self.p.history[S][-1][0], T-FIVE)
        self.p.candle_trade(S, T+FIVE+2000, 102, 1, T+FIVE-1, 4)  # Too late; rejected.
        self.p.tick(T+FIVE+2500)
        self.assertEqual(self.p.history[S][-1][1:5], [99, 101, 99, 100])

    def test_gap_restart_preserve_account_no_old_watch_or_same_quote_exit(self):
        self.p.seed(S, history(), T)
        self.buy()
        self.p.watch[S] = T-FIVE
        self.p.close(T+100)
        self.p = BollingerPaper(self.path, T+101)
        self.assertEqual(self.p.s['cash'], 2700000)
        self.assertFalse(self.p.watch)
        self.assertFalse(self.p.loaded)
        self.assertEqual(len(self.p.history[S]), HISTORY)
        self.p.on_book(S, T+102, book(T+102, stamp=T+100))
        self.assertIn(S, self.p.s['positions'])
        self.p.on_book(S, T+103, book(T+103))
        self.assertFalse(self.p.s['positions'])
        self.assertEqual(self.p.s['closed'], 1)

    def test_mid_bar_history_load_preserves_live_bridge_and_never_replays_closed_bar(self):
        self.p.connected([S], T-1000)  # The connection's partial candle is excluded.
        self.p.candle_trade(S, T+1000, 100.01, 1000, T+1000, 1)
        self.p.seed(S, history(), T+2000)
        self.assertEqual(self.p.active_since[S], T)
        self.p.tick(T+FIVE+2500)
        self.assertEqual(self.p.states[S], 'READY')
        self.assertEqual(self.p.watch[S], T)
        self.p.watch.clear()
        self.p.candle_trade(S, T+FIVE+3000, 100.3, 3000, T+FIVE+3000, 2)
        # History finishes just after close but before the settlement timer.
        self.p.seed(S, [], T+2*FIVE+1000)
        self.p.tick(T+2*FIVE+2500)
        self.assertFalse(self.p.watch)
        self.assertFalse(self.p.s['pending'])
        self.assertEqual(self.p.history[S][-1][0], T+FIVE)

    def test_readonly_results_button_status_and_daily_anchor(self):
        self.p.tick(T+10000)
        text, keyboard = view(self.root, T+10001, 'bollinger')
        self.assertTrue(text.startswith('• 누적 승률'))
        self.assertIn('더블볼린저·CCI', text)
        self.assertIn('과거봉 준비 1', text)
        self.assertIn('+0.00%', text)
        self.assertIn('performance:bollinger:0', str(keyboard))
        self.assertTrue(all(len(row) <= 3 for row in keyboard['inline_keyboard']))


class IntegrationTests(unittest.TestCase):
    def test_fast_reference_gate_does_not_cancel_bollinger_entry(self):
        with tempfile.TemporaryDirectory() as d:
            observer = Observer(Path(d)/'observe.sqlite3', T)
            p = observer.bollinger = BollingerPaper(Path(d)/'bb.sqlite3', T)
            observer.connected('g', [S], T)
            observer.repair_blocked.add(S)
            p.signal(S, T+1, 100, 98, {})
            observer.ingest(dict(type='trade', code=S, trade_timestamp=T+2,
                                 sequential_id=1, trade_price=100, trade_volume=1, ask_bid='BID'), T+2)
            observer.ingest(dict(type='orderbook', code=S, timestamp=T+3,
                                 orderbook_units=[dict(bid_price=100,ask_price=100.01,bid_size=100000,ask_size=100000)]), T+3)
            self.assertIn(S, p.s['positions'])
            observer.gap('g', T+4, 'TEST')
            self.assertFalse(p.loaded)
            self.assertEqual(p.s['positions'][S]['exit']['reason'], 'DATA_GAP_TEST')
            observer.db.close()
            p.close(T+5)

    def test_stale_unheld_trade_invalidates_bollinger_candles(self):
        with tempfile.TemporaryDirectory() as d:
            observer = Observer(Path(d)/'observe.sqlite3', T)
            p = observer.bollinger = BollingerPaper(Path(d)/'bb.sqlite3', T)
            observer.connected('g', [S], T)
            p.seed(S, history(), T)
            p.watch[S] = T-FIVE
            observer.ingest(dict(type='trade', code=S, trade_timestamp=T-3000), T)
            self.assertNotIn(S, p.loaded)
            self.assertNotIn(S, p.watch)
            observer.db.close()
            p.close(T+5)

    def test_history_io_is_asynchronous_bounded_and_never_decides(self):
        async def exercise(p):
            class Client:
                calls = 0
                def candles(self, symbol, cursor):
                    self.calls += 1
                    return history(cursor, 200)
            client = Client()
            task = asyncio.create_task(warm_history(p, [S], lambda:T+2500, client))
            # api_rows parsing is independently tested against real API fixtures.
            with patch('magi1.bollinger_paper.api_rows', side_effect=lambda payload,cursor:payload):
                for _ in range(300):
                    await asyncio.sleep(.01)
                    if S in p.loaded:
                        break
                self.assertIn(S, p.loaded)
                self.assertEqual(client.calls, 2)
                self.assertEqual(len(p.history[S]), 400)
                self.assertFalse(p.watch)
                self.assertFalse(p.s['pending'])
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        with tempfile.TemporaryDirectory() as d:
            p = BollingerPaper(Path(d)/'bb.sqlite3', T)
            p.connected([S], T)
            asyncio.run(exercise(p))
            p.close(T+5)


if __name__ == '__main__':
    unittest.main()
