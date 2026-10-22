"""Native REST repairs data only; new entries still require fresh live bars."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi1.bollinger_paper import BollingerPaper, FIVE, warm_history, features
from magi2.fast_models.bear import BearPaper
from tests.test_bollinger_paper import T, S, history


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = BollingerPaper(Path(self.tmp.name)/'bb.sqlite3', T)
        self.p.connected([S], T)

    def tearDown(self):
        self.p.close(T+20*FIVE)
        self.tmp.cleanup()

    def warm(self, rows, now=T+2500, paper=None, reconnect=False):
        paper = paper or self.p
        class Client:
            calls = []
            def candles(self, symbol, cursor):
                self.calls.append(cursor)
                if reconnect:
                    # Return a page whose generation was invalidated in flight.
                    paper.invalidate_candles(symbol, now+FIVE)
                return [r for r in rows if r[0] < cursor][-200:]
        client = Client()
        async def exercise():
            original_sleep = asyncio.sleep
            async def quick_sleep(seconds):
                await original_sleep(.001)
            with patch('magi1.bollinger_paper.api_rows', side_effect=lambda payload,cursor:payload), \
                    patch('magi1.bollinger_paper.asyncio.sleep', quick_sleep):
                task = asyncio.create_task(warm_history(paper, [S], lambda:now, client))
                try:
                    for _ in range(1000):
                        await original_sleep(.001)
                        if paper.history_updated == now or (reconnect and client.calls):
                            # Allow the page to finish and reject the old generation.
                            await original_sleep(.01)
                            break
                    else:
                        self.fail('history worker did not finish')
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        asyncio.run(exercise())
        return client.calls

    def test_missing_live_bar_repaired_without_replaying_signal(self):
        self.p.seed(S, history(), T)
        self.p.tick(T+FIVE+2500)  # A native trade existed but WS missed its bar.
        self.assertIn(S, self.p.needs_history)
        self.assertEqual(self.p.history_details[S]['reason'], 'REST_REPAIR_PENDING')
        self.warm(history(T+FIVE, 400), T+FIVE+3000)
        self.assertEqual(self.p.history_repaired, 1)
        self.assertIsNotNone(features(self.p.history[S]))
        self.assertFalse(self.p.watch)
        self.assertFalse(self.p.s['pending'])
        self.p.tick(T+FIVE+4000)
        self.assertFalse(self.p.watch)  # Repaired close cannot be replayed.
        ts = T+FIVE+5000
        self.p.candle_trade(S, ts, 100.01, 1000, ts, 1)
        self.p.tick(T+2*FIVE+2500)
        self.assertEqual(self.p.states[S], 'READY')

    def test_confirmed_no_trade_remains_missing_and_stops_repair_loop(self):
        rows = history(n=400)
        missing = rows[-20][0]
        rows = [r for r in rows if r[0] != missing]
        self.warm(rows)
        self.assertNotIn(S, self.p.needs_history)
        self.assertIn(missing, self.p.history_no_trade[S])
        self.assertNotIn(missing, {r[0] for r in self.p.history[S]})
        self.assertIsNone(features(self.p.history[S]))
        self.assertEqual(self.p.history_details[S]['reason'], 'CONFIRMED_NO_TRADE')
        self.p.assess(S, T-FIVE, T+3000)
        self.assertNotIn(S, self.p.needs_history)
        self.assertFalse(self.p.s['pending'])

    def test_bear_needs_one_page_and_reconnect_fetches_only_recent_bridge(self):
        bear = BearPaper(Path(self.tmp.name)/'bear.sqlite3', T)
        try:
            bear.connected([S], T)
            self.assertEqual(len(self.warm(history(n=400), paper=bear)), 1)
            self.assertEqual(bear.observation()['history_required'], 60)
            self.warm(history(n=400))
            self.p.invalidate_candles(S, T+FIVE)
            calls = self.warm(history(T+FIVE, 400), T+FIVE+2500)
            self.assertEqual(len(calls), 1)
            self.assertFalse(self.p.watch)
        finally:
            bear.close(T+20*FIVE)

    def test_older_unknown_hole_is_repaired_but_real_no_trade_is_not_requeried(self):
        rows = history(n=400)
        missing = rows[-300][0]
        self.p.seed(S, [r for r in rows if r[0] != missing], T)
        self.p.history_audited.add(S)
        self.p.assess(S, T-FIVE, T+1)
        self.assertEqual(len(self.warm(rows)), 2)
        self.assertIn(missing, {r[0] for r in self.p.history[S]})
        self.assertFalse(self.p.history_details)

    def test_disconnected_inflight_page_cannot_mark_history_ready(self):
        self.warm(history(n=400), reconnect=True)
        self.assertNotIn(S, self.p.loaded)
        self.assertNotIn(S, self.p.history_audited)
        self.assertFalse(self.p.history[S])

    def test_empty_api_response_does_not_claim_confirmed_no_trade(self):
        self.warm([])
        self.assertFalse(self.p.history_no_trade[S])
        self.assertEqual(self.p.history_details[S]['reason'], 'REST_REPAIR_PENDING')
        self.assertIn(S, self.p.needs_history)


if __name__ == '__main__':
    unittest.main()
