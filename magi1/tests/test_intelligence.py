import json
import tempfile
import unittest
from types import SimpleNamespace
from magi1.storage import Storage
from magi1.derived_signals import DerivedSignalEngine
from magi1.intelligence import publish_intelligence
from magi2.magi1_intelligence import load_intelligence


class IntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(self.temp.name)
        self.engine = DerivedSignalEngine(self.storage)

    def tearDown(self):
        self.storage.close()
        self.temp.cleanup()

    def feature(self, ts, ret, venue='upbit', imbalance=None):
        return SimpleNamespace(venue=venue, asset='BTC', horizon='MICRO',
            ts_ms=ts, return_bps=ret, volume_acceleration=4,
            book_imbalance=imbalance, coverage_ms=10000)

    def test_missing_book_and_independent_venues(self):
        for venue in ('upbit', 'binance'):
            for ts, ret in ((1000, 1), (2000, 4), (3000, 20)):
                self.engine.on_feature(self.feature(ts, ret, venue))
        rows = self.storage.query('derived_signal', 0, 4000)
        self.assertEqual({r['evidence']['venue'] for r in rows}, {'upbit','binance'})
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.storage.raw_rows, 0)

    def test_gap_duplicate_and_sustained_positive_return(self):
        for ts, ret in ((1000,1),(2000,4),(2000,100),(10000,200),(11000,200),(12000,200)):
            self.engine.on_feature(self.feature(ts, ret))
        rows=self.storage.query('derived_signal',0,20000)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['event_ts_ms'],12000)
        self.assertEqual(rows[0]['direction'],'BUY')

    def test_classified_whale_survives_handoff(self):
        row = dict(category='large_transfer', amount=500, source='onchain',
            provider='public', tx_hash='abc', received_ts_ms=3000,
            asset='BTC', direction='UNKNOWN', classification_basis='known_at_event',
            metadata={'transaction': {'input_labels': [{'status':'UNKNOWN'}]}})
        self.engine.on_onchain(row)
        path = publish_intelligence(self.storage, 4000)
        rows = load_intelligence(path, 4001)
        self.assertEqual(rows[0]['direction'], 'UNKNOWN')
        self.assertEqual(rows[0]['role'],'WAVE_INPUT')
        self.assertEqual(rows[0]['parent_strategy'],'WAVE')
        self.assertEqual(rows[0]['evidence']['classification_basis'], 'known_at_event')
        self.assertIsNone(rows[0]['calibrated_probability'])
        self.assertFalse(rows[0]['execution_eligible'])
        self.assertIn('input_labels', rows[0]['evidence']['transaction'])
        with self.assertRaisesRegex(ValueError, 'STALE'):
            load_intelligence(path, 124000)
        payload = json.loads(path.read_text())
        payload['observations'][0]['execution_eligible'] = True
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'AUTHORIZE'):
            load_intelligence(path, 4001)

    def test_empty_snapshot_and_invalid_values(self):
        for ts in (1000,2000,3000):
            self.engine.on_feature(self.feature(ts, float('nan')))
        path = publish_intelligence(self.storage, 4000)
        self.assertEqual(load_intelligence(path, 4001), [])
        with self.assertRaisesRegex(ValueError, 'FUTURE'):
            load_intelligence(path, 3999)

if __name__ == '__main__':
    unittest.main()
