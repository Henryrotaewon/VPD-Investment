import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from magi1.evaluation import evaluate
from magi1.collectors.public_ws import VENUE_ADAPTERS
from magi1.features import FeatureEngine
from magi1.formation import FormationEngine
from magi1.normalizer import book, split_symbol, trade
from magi1.storage import Storage
from magi1.report import build_daily
from magi1.universe import parse_markets
from magi1.vpd_join import VPDPointInTimeJoin


class Tests(unittest.TestCase):
    def test_normalization_and_exchange_local_timestamp(self):
        e=trade("binance","BTCUSDT","10","2",1000,"buy",received_ts_ms=1010)
        self.assertEqual((e.base,e.quote,e.exchange_ts_ms,e.received_ts_ms),("BTC","USDT",1000,1010))
        b=book("upbit","KRW-BTC",[(9,2)],[(11,3)],1000,received_ts_ms=1011)
        self.assertEqual(b.mid,10)

    def test_bidirectional_synthetic_state_machine(self):
        emitted=[]; engine=FormationEngine(lambda k,v: emitted.append((k,v)))
        features=FeatureEngine()
        for venue,side,p,ts in [("binance","BUY",100,1000),("binance","BUY",101,2000),("bybit","BUY",100,1000),("bybit","BUY",101,2100),("upbit","BUY",100,1000),("upbit","BUY",101,2200),("bithumb","BUY",100,1000),("bithumb","BUY",101,2300)]:
            for f in features.ingest(trade(venue,"BTC-USDT" if venue in {"binance","bybit"} else "BTC-KRW",p,1,ts,side,received_ts_ms=ts)): engine.ingest(f)
        states={x.state for _,x in emitted}; self.assertTrue({"FORMATION","GLOBAL_CONSENSUS","KOREA_EARLY_RECEPTION","PROPAGATION"}<=states)
        sell=[]; e2=FormationEngine(lambda k,v:sell.append(v)); ff=FeatureEngine()
        for p,ts in [(100,1000),(99,2000)]:
            for f in ff.ingest(trade("kraken","ETH-USD",p,1,ts,"SELL",received_ts_ms=ts)): e2.ingest(f)
        self.assertEqual(sell[0].direction,"SELL")

    def test_universe_parser(self):
        self.assertEqual(parse_markets("binance",[{"symbol":"BTCUSDT","quoteVolume":"12"}]),{"BTC":12.0})

    def test_point_in_time_vpd_no_lookahead(self):
        j=VPDPointInTimeJoin([{"ts_ms":100,"assets":{"BTC":{"vpd":70}}},{"ts_ms":200,"assets":{"BTC":{"vpd":90}}}])
        self.assertEqual(j.join(150,"BTC")["vpd"],70)

    def test_forward_windows_and_false_shock(self):
        rows=evaluate("x","FLOW_ONLY","BUY",100,[(0,100),(10_000,101),(3_600_000,99)],0)
        self.assertEqual([x.horizon_sec for x in rows],[10,30,60,300,1800,3600]); self.assertFalse(rows[0].false_shock); self.assertTrue(rows[-1].false_shock)

    def test_persistent_storage(self):
        with tempfile.TemporaryDirectory() as d:
            s=Storage(d); e=trade("x","BTC-USD",1,1,1,received_ts_ms=2); s.append_raw("trade",e); s.append("trade",e)
            self.assertEqual(len(s.query("trade",0,3)),1)

    def test_exchange_parser_smoke(self):
        messages={
            "binance":{"data":{"e":"trade","s":"BTCUSDT","p":"1","q":"2","T":1,"m":False}},
            "bybit":{"topic":"publicTrade.BTCUSDT","data":[{"s":"BTCUSDT","p":"1","v":"2","T":1,"S":"Buy"}]},
            "kraken":{"channel":"trade","data":[{"symbol":"BTC/USD","price":1,"qty":2,"timestamp":"1","side":"buy"}]},
            "upbit":{"type":"trade","code":"KRW-BTC","trade_price":1,"trade_volume":2,"trade_timestamp":1,"ask_bid":"BID"},
            "bithumb":{"type":"trade","code":"KRW-BTC","trade_price":1,"trade_volume":2,"trade_timestamp":1,"ask_bid":"BID"},
            "coinone":{"channel":"TRADE","data":{"quote_currency":"KRW","target_currency":"BTC","trades":[{"price":1,"qty":2,"timestamp":1}]}}
        }
        for venue,adapter in VENUE_ADAPTERS.items():
            self.assertEqual(adapter.parse(messages[venue],10)[0].venue,venue)

    def test_daily_report_smoke(self):
        with tempfile.TemporaryDirectory() as d:
            s=Storage(d); now=datetime(2026,9,15,7,0,tzinfo=timezone(timedelta(hours=9)))
            path=build_daily(s,now)
            self.assertIn("On-chain → Global → Derivatives → Korea → VPD → Price",path.read_text())


if __name__=="__main__": unittest.main()
