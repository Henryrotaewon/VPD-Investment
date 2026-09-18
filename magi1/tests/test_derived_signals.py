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
class FastDefinitionTests(unittest.TestCase):
 def run_features(self,returns,volume=3):
  with tempfile.TemporaryDirectory() as root:
   storage=Storage(root);engine=DerivedSignalEngine(storage)
   for i,ret in enumerate(returns):
    engine.on_feature(SimpleNamespace(venue='upbit',asset='BTC',horizon='MICRO',ts_ms=(i+1)*1000,
         return_bps=ret,volume_acceleration=volume,book_imbalance=None,coverage_ms=10000))
   rows=storage.query('derived_signal',0,100000);storage.close();return rows
 def test_slowing_decline_and_sell_are_not_fast(self):
  self.assertEqual(self.run_features([-100,-50,-20]),[])
  self.assertEqual(self.run_features([0,-30,-100]),[])
 def test_positive_but_small_or_no_volume_not_fast(self):
  self.assertEqual(self.run_features([1,4,10]),[])
  self.assertEqual(self.run_features([10,30,50],1.9),[])
 def test_rapid_rise_does_not_require_positive_acceleration(self):
  rows=self.run_features([50,40,30])
  self.assertEqual(len(rows),1);self.assertEqual(rows[0]['direction'],'BUY')
  self.assertEqual(rows[0]['evidence']['fast_rule_version'],'fast-rise-v1')
 def test_repeat_cooldown(self):
  self.assertEqual(len(self.run_features([30]*7)),1)

if __name__=="__main__":unittest.main()
