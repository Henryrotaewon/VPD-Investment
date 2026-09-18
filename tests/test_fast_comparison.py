import tempfile
import unittest
from magi2.fast_comparison import ForwardCheck,aggregate,report,VERSION
from magi3.fast_audit import FastAudit

class ComparisonTests(unittest.TestCase):
    def test_fixed_horizon_and_missing_not_failure(self):
        f=ForwardCheck(1000,100)
        self.assertIsNone(f.quote(300999,99))
        self.assertTrue(f.quote(301000,100)['non_rising'])
        self.assertIsNone(f.quote(302000,101))
        self.assertEqual(ForwardCheck(1000,100).quote(306001,99)['status'],'UNAVAILABLE')
        self.assertFalse(ForwardCheck(1000,100).quote(301000,100.1)['non_rising'])
    def test_dedup_pending_missing_denominators(self):
        def signal(i,t):return {'ts_ms':t,'signal_id':i,'event_type':'SIGNAL_DETECTED','venue':'upbit','rule_version':'fast-auto-v1','observed':{'selection_scan_ts_ms':100}}
        events=[signal('a',1),signal('a',2),signal('b',3),signal('c',499999),
                {'ts_ms':300001,'signal_id':'a','event_type':'SIGNAL_EVALUATED','venue':'upbit','rule_version':VERSION,'result':{'status':'EVALUATED','non_rising':True}},
                {'ts_ms':100,'signal_id':'scan','event_type':'SCAN_COMPLETED','venue':'upbit','rule_version':'fast-auto-v1','observed':{'status':'RANKED','ranked':200}}]
        r=aggregate(events,0,500000)[0]
        self.assertEqual((r['signals'],r['evaluated'],r['pending'],r['unavailable']),(3,1,1,1))
        self.assertEqual(r['non_rising_pct'],100)
        self.assertEqual(r['signals_per_1000_symbol_scans'],15)
        self.assertIsNone(aggregate(events,0,500000)[1]['non_rising_pct'])
    def test_empty_report_does_not_invent_performance(self):
        with tempfile.TemporaryDirectory() as root:
            audit=FastAudit(root+'/test.db')
            text=report(audit,1000000000);audit.db.close()
            self.assertIn('비상승률: —',text);self.assertIn('표본 부족',text)
            self.assertIn('수수료 미차감',text)
