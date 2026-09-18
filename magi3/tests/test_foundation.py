import os,unittest
from unittest.mock import patch
from magi3.config import Config
from magi3.models import OrderIntent
from magi3.risk import RiskSnapshot,check
from magi3.adapters.upbit import UpbitAdapter
from magi3.shadow import simulate_market

class TestFoundation(unittest.TestCase):
    def test_live_is_off_by_default(self):
        with patch.dict(os.environ,{},clear=True):
            c=Config.from_env()
        self.assertEqual(c.mode,"DRY_RUN");self.assertFalse(c.can_submit_live)

    def test_live_requires_explicit_arm(self):
        with patch.dict(os.environ,{"MAGI3_MODE":"LIVE","MAGI3_LIVE_ENABLED":"0"},clear=True):
            c=Config.from_env()
        self.assertFalse(c.can_submit_live)

    def test_risk_limits_order(self):
        with patch.dict(os.environ,{},clear=True):c=Config.from_env()
        i=OrderIntent("s1","upbit","KRW-BTC","BUY","MARKET",50000)
        ok,reasons=check(i,RiskSnapshot(),c)
        self.assertFalse(ok);self.assertIn("MAX_ORDER",reasons)

    def test_adapter_cannot_trade(self):
        i=OrderIntent("s1","upbit","KRW-BTC","BUY","MARKET",10000)
        x=UpbitAdapter().submit(i,live_allowed=False)
        self.assertEqual(x["status"],"BLOCKED")
        with self.assertRaises(RuntimeError):UpbitAdapter().submit(i,live_allowed=True)

    def test_shadow_walks_visible_asks(self):
        book=[{"orderbook_units":[
            {"ask_price":100.0,"ask_size":1.0,"bid_price":99.0,"bid_size":1.0},
            {"ask_price":101.0,"ask_size":2.0,"bid_price":98.0,"bid_size":2.0}]}]
        x=simulate_market(book,"BUY",150,fee_rate=0.0005)
        self.assertTrue(x.complete);self.assertGreater(x.avg_price,100.0);self.assertGreater(x.slippage_bps,0)

if __name__=="__main__":unittest.main()

