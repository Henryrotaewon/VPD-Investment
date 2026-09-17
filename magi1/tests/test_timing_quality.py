import asyncio
import unittest
from unittest.mock import patch

from magi1.collectors.public_ws import (
    CollectorSupervisor,
    BOOK_STALE_MS,
    TRADE_IDLE_MS,
)


class TimingQualityTests(unittest.TestCase):
    def make(self):
        async def sink(event):
            pass
        return CollectorSupervisor(['BTC'], sink)

    def test_sparse_trade_does_not_mark_venue_stale_if_book_is_fresh(self):
        c = self.make()
        current = 1_000_000
        for venue in ('binance','bybit','kraken','upbit','bithumb','coinone'):
            c.connected[venue] = True
            c.last_message[venue] = current - 10
            c.last_event[(venue,'BTC','BookEvent')] = current - 10
            c.last_event[(venue,'BTC','TradeEvent')] = current - TRADE_IDLE_MS - 1
        with patch('magi1.collectors.public_ws.now_ms', return_value=current):
            d = c.diagnostics()
        self.assertTrue(all(not x['stale'] for x in d.values()))
        self.assertTrue(all(x['trade_inactive_assets'] == ['BTC']
                            for x in d.values()))

    def test_stale_book_blocks_measurement_readiness(self):
        c = self.make()
        current = 1_000_000
        c.connected['upbit'] = True
        c.last_message['upbit'] = current - 10
        c.last_event[('upbit','BTC','BookEvent')] = current - BOOK_STALE_MS - 1
        c.last_event[('upbit','BTC','TradeEvent')] = current - 10
        with patch('magi1.collectors.public_ws.now_ms', return_value=current):
            d = c.diagnostics()['upbit']
        self.assertTrue(d['stale'])
        self.assertFalse(d['measurement_ready'])
        self.assertEqual(d['book_stale_assets'], ['BTC'])

    def test_binance_partial_depth_missing_timestamp_is_expected(self):
        c = self.make()
        c.missing_ts['binance'] = 100
        c.expected_missing_ts['binance'] = 100
        self.assertEqual(c.unexpected_missing_ts['binance'], 0)

    def test_timing_distribution_is_reported_by_event_kind(self):
        c = self.make()
        current = 1_000_000
        c.connected['kraken'] = True
        c.last_message['kraken'] = current - 1
        c.last_event[('kraken','BTC','BookEvent')] = current - 1
        c.last_event[('kraken','BTC','TradeEvent')] = current - 1
        c.offsets[('kraken','TradeEvent')].extend([5, 10, 20, 40])
        c.offsets[('kraken','BookEvent')].extend([4, 8, 16, 32])
        with patch('magi1.collectors.public_ws.now_ms', return_value=current):
            d = c.diagnostics()['kraken']
        self.assertEqual(d['lead_time_clock'], 'received_ts_ms')
        self.assertEqual(d['timing_quality']['TradeEvent']['samples'], 4)
        self.assertIsNotNone(
            d['timing_quality']['TradeEvent']['spread_p95_p05_ms'])


if __name__ == '__main__':
    unittest.main()
