import tempfile
import unittest
from pathlib import Path
from magi1.storage import Storage
from magi1.retire_wave import retire, KINDS
from magi1.research import ResearchEngine
from magi1.schema import BookEvent,BookLevel,TradeEvent

class RetirementTests(unittest.TestCase):
    def test_scoped_idempotent_cleanup_preserves_vpd_raw_and_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=Storage(root)
            for kind in KINDS:s.append(kind,dict(event_ts_ms=10))
            s.append('evaluation',dict(event_ts_ms=10,cohort='FLOW_ONLY'))
            s.append('evaluation',dict(event_ts_ms=10,cohort='VPD_ONLY'))
            s.append('cohort_entry',dict(event_ts_ms=10,cohorts=['FLOW_VPD']))
            s.append('cohort_entry',dict(event_ts_ms=10,cohorts=['FLOW_VPD','VPD_ONLY']))
            s.append('vpd_snapshot',dict(event_ts_ms=10))
            (s.raw/'trade-test.gz').write_bytes(b'raw')
            (root/'exports').mkdir();(root/'exports'/'wave_latest.json').write_text('{}')
            (root/'exports'/'intelligence_latest.json').write_text('{}')
            (root/'reports').mkdir();p=root/'reports'/'magi1_daily_20260925.md'
            p.write_text('# MAGI1 Daily Crypto Shock Report\nWindow: yesterday\n| FLOW_ONLY | 1 |\n| VPD_ONLY | 2 |\n')
            r=retire(s,20)
            self.assertEqual(r['deleted_records']['flow_evaluations'],1)
            self.assertEqual(s.query('evaluation',0,30)[0]['cohort'],'VPD_ONLY')
            self.assertEqual(len(s.query('cohort_entry',0,30)),1)
            self.assertTrue((s.raw/'trade-test.gz').exists())
            self.assertTrue((root/'exports'/'intelligence_latest.json').exists())
            self.assertFalse((root/'exports'/'wave_latest.json').exists())
            self.assertIn('VPD_ONLY',p.read_text());self.assertNotIn('FLOW_ONLY',p.read_text())
            self.assertEqual(retire(s,30),r);s.close()
    def test_disabled_wave_does_not_recreate_propagation_but_keeps_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Storage(tmp);e=ResearchEngine(s,['BTC'],0,wave_enabled=False)
            from unittest.mock import Mock
            e.formation=Mock()
            for ts in range(1000,14000,1000):
                e.ingest(BookEvent('upbit','KRW-BTC','BTC','KRW',ts,ts,[BookLevel(100,1)],[BookLevel(101,1)]))
                e.ingest(TradeEvent('upbit','KRW-BTC','BTC','KRW',ts,ts,100,1,'BUY',str(ts)))
                e.tick(ts)
            e.formation.ingest.assert_not_called();e.formation.tick.assert_not_called()
            self.assertTrue(e.features.books);self.assertTrue(s.raw_rows or list(s.raw.glob('*')));s.close()
