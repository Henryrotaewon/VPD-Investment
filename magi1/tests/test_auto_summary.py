import tempfile
import unittest
from magi1.storage import Storage
from magi1.auto_summary import build_summary


class SummaryTests(unittest.TestCase):
    def test_legacy_and_missing_addresses_are_visible_without_pooling_versions(self):
        with tempfile.TemporaryDirectory() as root:
            s=Storage(root)
            s.append('onchain_candidate', {'event_ts_ms':10, 'metadata':{}})
            s.append('onchain_candidate', {'event_ts_ms':11, 'metadata':{'transaction':{
                'inputs':[{'address':'a'}, {'address':None}],
                'input_labels':[{'status':'UNKNOWN'}]}}})
            edge=dict(event_ts_ms=12,asset='BTC',direction='BUY',horizon='MICRO',origin_venue='binance',follower_venue='upbit',received=True)
            s.append('propagation',edge)
            s.append('propagation',{**edge,'coverage_version':'quote-v2'})
            for ident,status in [('a','RECEIVED'),('b','NON_REACTION'),('c','UNOBSERVABLE'),('a','RECEIVED')]:
                s.append('reception_trial_v3',{**edge,'shock_id':ident,'status':status})
            r=build_summary(s,100)
            self.assertEqual(r['onchain']['missing_transaction_structure'],1)
            self.assertEqual(r['onchain']['inputs_with_address'],1)
            self.assertEqual(r['edges'][0]['observed_n'],1)
            self.assertEqual(r['onchain']['label_UNKNOWN'],1)
            coverage=r['coverage_edges'][0]
            self.assertEqual(coverage['total'],3)
            self.assertEqual(coverage['observed_probability'],.5)
            self.assertEqual(coverage['missing_outcome_bounds'],[1/3,2/3])
            s.close()
