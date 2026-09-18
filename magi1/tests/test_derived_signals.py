import tempfile,unittest
from types import SimpleNamespace
from magi1.storage import Storage
from magi1.derived_signals import DerivedSignalEngine

class T(unittest.TestCase):
 def test_fast_from_existing_features(self):
  with tempfile.TemporaryDirectory() as d:
   s=Storage(d);e=DerivedSignalEngine(s)
   for ts,r,va,bi in [(1000,1,1,0.1),(2000,4,2,0.2),(3000,20,4,0.5)]:
    e.on_feature(SimpleNamespace(venue="upbit",asset="BTC",horizon="MICRO",ts_ms=ts,return_bps=r,volume_acceleration=va,book_imbalance=bi,coverage_ms=10000))
   s.flush();self.assertTrue(s.query("derived_signal",0,10000));s.close()
 def test_whale_from_existing_candidate(self):
  with tempfile.TemporaryDirectory() as d:
   s=Storage(d);e=DerivedSignalEngine(s)
   x=SimpleNamespace(category="large_transfer",amount=500,source="onchain",provider="p",tx_hash="x",received_ts_ms=1,asset="BTC",direction="UNKNOWN",metadata={"label":"unclassified_public_raw"})
   e.on_onchain(x);s.flush();z=s.query("derived_signal",0,10)[0]
   self.assertEqual(z["signal_type"],"WHALE");self.assertEqual(z["direction"],"UNKNOWN");s.close()
if __name__=="__main__":unittest.main()
