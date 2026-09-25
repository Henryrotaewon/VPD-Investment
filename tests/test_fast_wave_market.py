from copy import deepcopy
from datetime import datetime, timezone
import tempfile
import unittest

from magi2.fast_wave_indicator import DAY_MS, VENUES
from magi2.fast_wave_market import IndicatorMarket, normalize
from magi2.fast_wave_probe import observe_one
from magi2.fast_wave_store import EvidenceStore


def payload(venue):
    rows = []
    for i in range(121):
        if venue in ('upbit', 'bithumb'):
            rows.append(dict(candle_date_time_utc=datetime.fromtimestamp(i * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
                             high_price=110, low_price=90, trade_price=100, candle_acc_trade_volume=1000))
        elif venue == 'binance':
            rows.append([i * DAY_MS, '100', '110', '90', '100', '1000', (i + 1) * DAY_MS - 1])
        else:
            rows.append([i * 86400, '100', '110', '90', '100', '100', '1000', 30])
    return {'error': [], 'result': {'XXBTZUSD': rows, 'last': 0}} if venue == 'kraken' else rows


class MarketTests(unittest.TestCase):
    def test_all_four_adapters_and_cached_baseline(self):
        for venue in VENUES:
            with self.subTest(venue=venue):
                response = payload(venue)
                ticks = [120 * DAY_MS + 600000]
                market = IndicatorMarket(venue, clock=lambda: ticks[0])
                self.addCleanup(market.http.close)
                market.symbols = {'TEST': 'TEST'}
                calls = []
                def get(path, params):
                    calls.append((path, params))
                    return deepcopy(response)
                market.get = get
                first = market.observe('TEST')
                ticks[0] += 60000
                second = market.observe('TEST')
                self.assertEqual(first['baseline'], second['baseline'])
                self.assertEqual(first['baseline_ms'], 119 * DAY_MS)
                self.assertEqual(first['day_ms'], 120 * DAY_MS)
                if venue != 'kraken':
                    key = 'limit' if venue == 'binance' else 'count'
                    self.assertEqual(calls[0][1][key], 121)
                    self.assertEqual(calls[1][1][key], 2)
                else:
                    self.assertEqual(calls[1][1]['since'], 119 * 86400)

    def test_duplicate_missing_current_and_ambiguous_pair_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            normalize('binance', payload('binance') + payload('binance')[-1:])
        ambiguous = payload('kraken')
        ambiguous['result']['OTHER'] = []
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS'):
            normalize('kraken', ambiguous)
        market = IndicatorMarket('binance', clock=lambda: 120 * DAY_MS + 600000)
        self.addCleanup(market.http.close)
        market.symbols = {'TEST': 'TEST'}
        market.get = lambda *args: payload('binance')[:-1]
        with self.assertRaisesRegex(ValueError, 'CURRENT_DAY_CANDLE_MISSING'):
            market.observe('TEST')

    def test_delayed_response_and_history_revision_rejected(self):
        tick = [120 * DAY_MS + 600000]
        market = IndicatorMarket('binance', clock=lambda: tick[0])
        self.addCleanup(market.http.close)
        market.symbols = {'TEST': 'TEST'}
        response = payload('binance')
        market.get = lambda *args: deepcopy(response)
        market.observe('TEST')
        response[-2][4] = '101'
        with self.assertRaisesRegex(ValueError, 'BASELINE_REVISED'):
            market.observe('TEST')
        self.assertNotIn('TEST', market.cache)
        def slow(*args):
            tick[0] += 3001
            return deepcopy(response)
        market.get = slow
        with self.assertRaisesRegex(ValueError, 'SLOW_OR_CROSS_DAY'):
            market.observe('TEST')

    def test_probe_persists_evidence_without_order_calls(self):
        tick = [120 * DAY_MS + 600000]
        market = IndicatorMarket('binance', clock=lambda: tick[0])
        self.addCleanup(market.http.close)
        market.symbols = {'TEST': 'TEST'}
        market.get = lambda *args: payload('binance')
        market.flow = lambda *args: self.fail('Flat indicators must not query flow')
        with tempfile.TemporaryDirectory() as root:
            store = EvidenceStore(root, 'fixture')
            try:
                for _ in range(3):
                    result = observe_one(market, store, 'TEST')
                    tick[0] += 60000
                self.assertTrue(result['ready'])
                self.assertFalse(result['paper_candidate'])
                self.assertEqual(result['mode'], 'SHADOW')
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0], 3)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
