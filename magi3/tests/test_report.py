import unittest
from magi3.portfolio import Position,VenuePortfolio
from magi3.report import build_report,render_text
class T(unittest.TestCase):
 def test_integrated_report_and_attribution(self):
  vs=[VenuePortfolio("upbit",100,[Position("upbit","BTC",1,100,110,strategies=("VPD","FAST"))]),VenuePortfolio("binance",50,[Position("binance","ETH",2,50,55,strategies=("WAVE",))])]
  r=build_report(vs)
  self.assertEqual(len(r["venues"]),2);self.assertIn("VPD",r["strategy_summary"]);self.assertIn("WAVE",r["strategy_summary"])
  self.assertEqual(r["venues"][0]["positions"][0]["pick_basis"],["VPD","FAST"])
  s=render_text(r);self.assertIn("STRATEGY ATTRIBUTION",s);self.assertIn("PICK=VPD+FAST",s)
if __name__=="__main__":unittest.main()
