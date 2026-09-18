import os,unittest
from unittest.mock import patch
from magi3.config import Config
from magi3.models import OrderIntent
from magi3.risk import RiskSnapshot,check
from magi3.adapters.upbit import UpbitAdapter

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

if __name__=="__main__":unittest.main()
