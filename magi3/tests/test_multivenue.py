import os,unittest
from unittest.mock import patch
from magi3.adapters.base import usable_secret,require_credentials,CredentialsNotReady
from magi3.contracts import parse_strategy_signal
from magi3.venue import normalize_upbit_like,normalize_binance

class TestMultiVenue(unittest.TestCase):
    def test_placeholder_one_is_never_a_real_key(self):
        with patch.dict(os.environ,{"BINANCE_API_KEY":"1","BINANCE_SECRET_KEY":"1"},clear=True):
            self.assertFalse(usable_secret("BINANCE_API_KEY"))
            with self.assertRaises(CredentialsNotReady):require_credentials("BINANCE_API_KEY","BINANCE_SECRET_KEY")
    def test_strategy_signal_contract(self):
        s=parse_strategy_signal({"signal_id":"x","created_ts_ms":1,"asset":"BTC","side":"buy","strategy":"FAST","confidence":88,"expected_move_bps":90,"max_holding_sec":300,"preferred_venues":["upbit","binance"]})
        self.assertEqual(s.side,"BUY");self.assertEqual(s.preferred_venues,("upbit","binance"))
    def test_books_normalize(self):
        u=normalize_upbit_like("upbit","KRW-BTC",[{"orderbook_units":[{"bid_price":99,"ask_price":100}]}])
        b=normalize_binance("BTCUSDT",{"bids":[["99","1"]],"asks":[["100","1"]]})
        self.assertEqual(u.best_bid,b.best_bid);self.assertGreater(u.spread_bps,0)

if __name__=="__main__":unittest.main()
