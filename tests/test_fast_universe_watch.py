import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from magi1.fast_observe import Observer, DAY
from magi1.fast_paper import Paper
from magi1.bollinger_paper import BollingerPaper
from magi2.fast_models.bear import BearPaper
from magi1.fast_universe_watch import UniverseWatch, parse_markets

T = 200 * DAY


class UniverseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.o = Observer(self.path/'observe.db', T)
        self.o.paper = Paper(self.path/'paper.db', T)
        self.o.bollinger = BollingerPaper(self.path/'bollinger.db', T)
        self.bear = BearPaper(self.path/'bear.db', T)
        self.symbols = ['KRW-BTC']
        self.connections = []
        self.logs = []
        self.stamp = T+1

        async def stream(group, names):
            self.connections.append((group, names))
            self.o.connected(group, names, self.stamp)
            self.bear.connected(names, self.stamp)
            await asyncio.Event().wait()

        self.w = UniverseWatch(self.o, self.symbols, stream,
                               lambda: self.stamp, self.logs.append)
        await self.w.start()

    async def asyncTearDown(self):
        await self.w.close()
        for p in [self.o.paper, self.o.bollinger, self.bear]:
            p.db.close()
        self.o.db.close()
        self.tmp.cleanup()

    async def test_add_receives_new_feed_without_resetting_old_or_starting_fast(self):
        self.o.disable_fast(T+2)
        self.o.last_price['KRW-BTC'] = (T+2, 100, T+2)
        self.o.books['KRW-BTC'] = {'ts': T+2}
        old_since = self.o.symbol_since['KRW-BTC']
        before_cash = self.o.paper.s['cash']
        self.stamp = T+1000
        self.assertEqual(await self.w.refresh(['KRW-BTC', 'KRW-KAIA']), ['KRW-KAIA'])
        self.assertEqual(len(self.connections), 2)
        self.assertEqual(self.o.symbol_since['KRW-BTC'], old_since)
        self.assertEqual(self.o.last_price['KRW-BTC'][1], 100)
        self.assertEqual(self.o.books['KRW-BTC']['ts'], T+2)
        self.assertEqual(self.symbols, ['KRW-BTC', 'KRW-KAIA'])
        self.assertIn('KRW-KAIA', self.o.bollinger.needs_history)
        self.assertIn('KRW-KAIA', self.bear.needs_history)
        self.assertEqual(self.bear.states['KRW-KAIA'], 'HISTORY_WARMUP')
        t = self.stamp+1
        self.o.ingest(dict(type='trade', code='KRW-KAIA', trade_timestamp=t,
            sequential_id=10, trade_price=80, trade_volume=1, ask_bid='BID',
            stream_type='REALTIME'), t)
        self.o.ingest(dict(type='orderbook', code='KRW-KAIA', timestamp=t,
            orderbook_units=[dict(bid_price=79.9, ask_price=80,
                                 bid_size=1000, ask_size=1000)]), t)
        report = self.o.report()
        self.assertTrue(report['fast_disabled'])
        self.assertTrue(report['universe']['recent_feeds']['KRW-KAIA']['connected'])
        self.assertEqual(report['universe']['recent_feeds']['KRW-KAIA']['trade_ms'], t)
        self.assertEqual(report['universe']['recent_feeds']['KRW-KAIA']['book_ms'], t)
        self.assertEqual(self.o.paper.s['cash'], before_cash)
        self.assertFalse(self.o.paper.s['pending'])
        self.assertEqual(report['captures'], 0)
        self.assertTrue(self.o.paper.s['entries_paused'])

    async def test_repeated_poll_deduplicates_and_missing_row_does_not_remove_stream(self):
        await self.w.refresh(['KRW-BTC', 'KRW-KAIA'])
        tasks = dict(self.w.tasks)
        self.assertEqual(await self.w.refresh(['KRW-KAIA', 'KRW-BTC', 'KRW-KAIA']), [])
        await self.w.refresh(['KRW-BTC'])
        self.assertEqual(tasks, self.w.tasks)
        self.assertIn('KRW-KAIA', self.symbols)
        self.assertEqual(self.o.universe_status['retained_absent'], ['KRW-KAIA'])

    async def test_failed_poll_retains_streams_and_next_success_recovers(self):
        tasks = dict(self.w.tasks)
        last_success = self.o.universe_status['last_success_ms']
        with patch('magi1.fast_universe_watch.fetch_markets', new=AsyncMock(side_effect=ValueError('bad'))):
            await self.w.check(None)
        self.assertEqual(tasks, self.w.tasks)
        self.assertTrue(all(not t.done() for t in tasks.values()))
        self.assertEqual(self.o.universe_status['last_success_ms'], last_success)
        self.assertEqual(self.o.universe_status['status'], 'RETRY')
        with patch('magi1.fast_universe_watch.fetch_markets', new=AsyncMock(return_value=['KRW-BTC', 'KRW-KAIA'])):
            await self.w.check(None)
        self.assertEqual(self.o.universe_status['status'], 'OK')
        self.assertNotIn('error', self.o.universe_status)

    async def test_new_groups_are_bounded_and_existing_group_is_not_reconnected(self):
        added = [f'KRW-X{i}' for i in range(101)]
        await self.w.refresh(self.symbols+added)
        self.assertEqual([len(names) for _, names in self.connections], [1, 100, 1])
        self.assertEqual(self.connections[0][1], ('KRW-BTC',))
        self.assertEqual(len(self.o.symbol_group), 102)

    async def test_restart_finds_added_market_from_persisted_universe(self):
        logs = []
        other = UniverseWatch(self.o, ['KRW-BTC', 'KRW-KAIA'],
                               lambda group, names: asyncio.Event().wait(),
                               lambda: T+2000, logs.append)
        try:
            await other.start()
            self.assertEqual(logs[0]['symbols'], ['KRW-KAIA'])
            self.assertEqual(logs[0]['phase'], 'STARTUP')
        finally:
            await other.close()

    async def test_cancellation_cleans_up_all_subscription_tasks(self):
        await self.w.close()
        self.assertTrue(all(t.done() for t in self.w.tasks.values()))


class ParseTests(unittest.TestCase):
    def test_only_krw_deduplicated(self):
        self.assertEqual(parse_markets([{'market': 'BTC-X'}, {'market': 'KRW-KAIA'},
            {'market': 'KRW-KAIA'}, {'market': 'USDT-KAIA'}]), ['KRW-KAIA'])

    def test_invalid_empty_payload_rejected_before_mutation(self):
        for rows in [[], {}, [{'market': 'BTC-X'}], [{'bad': 'KRW-X'}],
                     [{'market': 'KRW-X'}, None], [{'market': 'KRW-'}]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_markets(rows)


if __name__ == '__main__':
    unittest.main()
