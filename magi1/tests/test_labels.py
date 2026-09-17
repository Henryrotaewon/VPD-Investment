import tempfile
import unittest
from pathlib import Path

from magi1.labels import AddressLabelRegistry, reclassify_onchain


class LabelTests(unittest.TestCase):
    def test_event_asof_does_not_use_future_label(self):
        with tempfile.TemporaryDirectory() as d:
            r=AddressLabelRegistry(Path(d)/'labels.jsonl')
            r.add({'chain':'BTC','address':'A','entity':'ExchangeA','role':'HOT','evidence_grade':'A_OFFICIAL','source':'official','known_at_ms':2000,'valid_from_ms':0})
            tx={'tx_hash':'t','inputs':[{'address':'A','value_sats':10}], 'outputs':[]}
            row={'event_ts_ms':1000,'metadata':{'transaction':tx}}
            historical=reclassify_onchain(row,r,'event',3000)
            current=reclassify_onchain(row,r,'current',3000)
            self.assertEqual(historical['metadata']['transaction']['input_labels'][0]['status'],'UNKNOWN')
            self.assertEqual(current['metadata']['transaction']['input_labels'][0]['entity'],'ExchangeA')
            self.assertEqual(historical['classification_basis'],'known_at_event')

    def test_conflicting_labels_are_not_silently_resolved(self):
        r=AddressLabelRegistry()
        base={'chain':'BTC','address':'A','role':'HOT','evidence_grade':'B_INDEPENDENT','known_at_ms':1,'valid_from_ms':0}
        r.add({**base,'entity':'ExchangeA','source':'provider-1'},persist=False)
        r.add({**base,'entity':'ExchangeB','source':'provider-2'},persist=False)
        x=r.resolve('BTC','A',100)
        self.assertEqual(x['status'],'CONFLICT')
        self.assertIsNone(x['entity'])

    def test_validity_window_is_separate_from_known_at(self):
        r=AddressLabelRegistry()
        r.add({'chain':'BTC','address':'A','entity':'ExchangeA','role':'COLD','evidence_grade':'A_OFFICIAL','source':'official','known_at_ms':100,'valid_from_ms':500,'valid_to_ms':1000},persist=False)
        self.assertEqual(r.resolve('BTC','A',400)['status'],'UNKNOWN')
        self.assertEqual(r.resolve('BTC','A',700)['entity'],'ExchangeA')
        self.assertEqual(r.resolve('BTC','A',1000)['status'],'UNKNOWN')


if __name__=='__main__': unittest.main()
