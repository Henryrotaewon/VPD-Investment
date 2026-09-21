import tempfile
import unittest
from pathlib import Path
from magi3.fast_audit import FastAudit
from magi2.fast_captures import flow_metrics,strength,AlertGate,captures,FILTER_VERSION
from magi2.telegram_ui import main_keyboard,parse_command
from magi2.fast_monitor import PublicMarket
from unittest.mock import patch

class CapturesTests(unittest.TestCase):
    def test_flow_is_weighted_and_requires_fresh_sample(self):
        rows=[(10000+i*500,100,1,i<15) for i in range(20)]
        flow=flow_metrics(rows,20000)
        self.assertEqual(flow['buyer_share_pct'],75)
        self.assertTrue(strength(flow,{'spread_bps':5,'breakout_bps':15})['strong'])
        self.assertFalse(strength(flow,{'spread_bps':11,'breakout_bps':15})['strong'])
        self.assertFalse(flow_metrics(rows,24000)['sufficient'])
        self.assertFalse(flow_metrics(rows[:5],20000)['sufficient'])
        self.assertIsNone(flow_metrics([],20000)['buyer_share_pct'])
    def test_venue_trade_side_parsing(self):
        samples={
            'upbit':[{'timestamp':10000+i*500,'trade_price':100,'trade_volume':1,'ask_bid':'BID'} for i in range(20)],
            'bithumb':[{'timestamp':10000+i*500,'trade_price':100,'trade_volume':1,'ask_bid':'BID'} for i in range(20)],
            'binance':[{'T':10000+i*500,'p':'100','q':'1','m':False} for i in range(20)],
            'kraken':{'result':{'XXBTZUSD':[['100','1',(10000+i*500)/1000,'b'] for i in range(20)],'last':'20'}}}
        for venue,raw in samples.items():
            with patch.object(PublicMarket,'get',return_value=raw),patch('magi2.fast_monitor.now',return_value=20000):
                self.assertEqual(PublicMarket(venue).buying_flow('A')['buyer_share_pct'],100)
    def test_durable_cross_venue_and_global_limits(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'a.db';a=FastAudit(path);g=AlertGate(a)
            self.assertIsNone(g.claim('a','upbit','KRW-BTC','BTC',10000000))
            a.db.close();a=FastAudit(path);g=AlertGate(a)
            self.assertEqual(g.claim('b','kraken','XXBTZUSD','XBT',10600000),'SAME_ASSET_1H')
            self.assertEqual(g.claim('c','upbit','KRW-ETH','ETH',10000100),'GLOBAL_10_MINUTE_GAP')
            self.assertIsNone(g.claim('d','upbit','KRW-ETH','ETH',10600000))
            self.assertIsNone(g.claim('e','upbit','KRW-SOL','SOL',11200000))
            self.assertEqual(g.claim('f','upbit','KRW-XRP','XRP',11800000),'GLOBAL_3_PER_HOUR')
            a.db.close()
    def test_latest_day_dedup_and_no_invented_probability(self):
        with tempfile.TemporaryDirectory() as root:
            a=FastAudit(Path(root)/'a.db');now=200000000
            obs={'asset':'BTC','ask':100,'returns_bps':{'5':150},'spread_bps':5,
                 'strength':{'version':FILTER_VERSION,'strong':True,'score':80,'flow':{'buyer_share_pct':80,'sample_trades':25}}}
            for ident,t in [('old',now-86400001),('earlier',now-200000),('new',now-100000)]:
                a.record('SIGNAL_DETECTED',ident,'upbit','KRW-BTC','ALERT_ONLY',ts_ms=t,rule_version='fast-price-rise-v4',observed=obs)
            text,markup=captures(a,now)
            self.assertIn('1종목',text);self.assertEqual(text.count('업비트 · BTC'),1)
            self.assertNotIn('강도',text);self.assertNotIn('수익률',text)
            self.assertIn('07:30',text);a.db.close()
        self.assertIn('FAST 모의투자',str(main_keyboard()));self.assertNotIn('FAST 포착',str(main_keyboard()))
        self.assertEqual(parse_command('FAST 포착'),'fast_captures')

