import copy
from dataclasses import replace
import math
import tempfile
import unittest
from magi1.fast_universe import universe
from magi1.fast_capture import parse
from magi1.fast_features import Features
from magi2.fast_lab.replay import Lab,Policy,Trial,candidate_reason


def trade(sec,price=100,qty=1,ident=None,side='BUY',venue='upbit',symbol='KRW-TEST'):
    return {'kind':'trade','venue':venue,'symbol':symbol,'asset':'TEST','quote':'KRW',
            'received_ts_ms':sec*1000,'event_ts_ms':sec*1000,'trade_id':ident or sec+1,
            'price':price,'qty':qty,'side':side}


def book(ts,bid=100,ask=100.01,size=10000,venue='upbit',symbol='KRW-TEST'):
    return {'kind':'book','venue':venue,'symbol':symbol,'asset':'TEST','quote':'KRW',
            'received_ts_ms':ts,'bids':[[bid,size]],'asks':[[ask,size]]}


def feature(ts=100000):
    return {'venue':'upbit','symbol':'KRW-TEST','asset':'TEST','quote':'KRW','received_ts_ms':ts,
            'return_10s_bps':40.,'volume_ratio':3.,'buy_notional_ratio':.8,'spread_bps':1.,'baseline_volatility_10s_bps':1.}


class UniverseTests(unittest.TestCase):
    def test_independent_venue_symbols_warnings_and_explicit_cap(self):
        u=universe('upbit',[{'market':'KRW-LOCALONLY'},{'market':'KRW-BTC'},{'market':'BTC-ETH'},
            {'market':'KRW-RISK','market_event':{'warning':True}},{'market':'KRW-USDT'}],1)
        self.assertEqual({m['asset'] for m in u['markets']},{'LOCALONLY','BTC'})
        self.assertEqual(u['scope'],'ALL_ELIGIBLE')
        capped=universe('upbit',[{'market':'KRW-A'},{'market':'KRW-B'}],1,1)
        self.assertEqual(capped['scope'],'EXPLICIT_TEST_CAP');self.assertEqual(capped['eligible_count'],2)
        b=universe('binance',{'symbols':[{'symbol':'ONLYUSDT','baseAsset':'ONLY','quoteAsset':'USDT','status':'TRADING','isSpotTradingAllowed':True}]},1)
        self.assertEqual(b['markets'][0]['asset'],'ONLY')
    def test_normalizers_side_and_quote(self):
        m={'KRW-TEST':{'symbol':'KRW-TEST','asset':'TEST','venue':'upbit','quote':'KRW'}}
        payload={'type':'trade','code':'KRW-TEST','sequential_id':1,'trade_price':100,'trade_volume':2,
                 'ask_bid':'BID','best_bid_price':99,'best_bid_size':3,'best_ask_price':100,'best_ask_size':4}
        rows=parse('upbit',payload,m,123)
        self.assertEqual(rows[0]['side'],'BUY');self.assertEqual(rows[1]['bids'],[[99.,3.]])
        payload['stream_type']='SNAPSHOT';self.assertEqual(parse('upbit',payload,m,123),[])
        m={'TESTUSDT':{'symbol':'TESTUSDT','asset':'TEST','venue':'binance','quote':'USDT'}}
        row=parse('binance',{'e':'trade','s':'TESTUSDT','t':1,'p':'1','q':'2','m':True},m,123)[0]
        self.assertEqual(row['side'],'SELL')


class FeatureTests(unittest.TestCase):
    def test_causal_warmup_volume_flow_and_no_future_dependency(self):
        engine=Features();last=None
        for sec in range(81):
            price=100 if sec<71 else 100+(sec-70)*.04
            engine.observe(book(sec*1000,price-.01,price+.01))
            last=engine.observe(trade(sec,price,5 if sec>=71 else 1))
            if sec<70:self.assertIsNone(last)
        self.assertGreater(last['return_10s_bps'],30)
        self.assertGreater(last['volume_ratio'],4);self.assertEqual(last['buy_notional_ratio'],1)
        saved=copy.deepcopy(last)
        engine.observe(trade(82,1000,10000));self.assertEqual(last,saved)
    def test_downturn_duplication_and_book_staleness(self):
        engine=Features()
        for sec in range(81):
            engine.observe(book(sec*1000,99,100))
            f=engine.observe(trade(sec,100-sec*.01))
        self.assertLess(f['return_10s_bps'],0)
        self.assertIsNone(engine.observe(trade(80,100,10000)))
        self.assertEqual(engine.quality['duplicate_or_reordered_trade'],1)
        f=engine.observe(trade(83,100));self.assertIsNone(f['spread_bps'])
        engine.reset('upbit');self.assertIsNone(engine.observe(trade(84)))
    def test_invalid_market_data_fail_closed(self):
        engine=Features()
        for price in (float('nan'),float('inf'),-1):
            with self.assertRaises(ValueError):engine.observe(trade(1,price))
        with self.assertRaises(ValueError):engine.observe(book(1000,101,100))


class TrialTests(unittest.TestCase):
    def test_latency_fees_and_exit_latency(self):
        p=Policy(latency_ms=500,fee_bps=10,slippage_bps=5,take_profit_bps=30)
        t=Trial(feature(),p)
        t.book(book(100100,99,100));self.assertIsNone(t.row['entry'])
        t.book(book(100500,99,100));self.assertEqual(t.state,'OPEN')
        expected=10000*1.0005*1.001
        self.assertAlmostEqual(t.row['entry']['cost_quote'],expected)
        t.book(book(101000,101,101.01));self.assertEqual(t.state,'EXIT_PENDING')
        self.assertIsNone(t.row['policy_exit'])
        t.book(book(101500,100.5,100.51))
        actual=(100*100.5*.9995*.999/expected-1)*10000
        self.assertAlmostEqual(t.row['policy_exit']['net_return_bps'],actual)
        self.assertEqual(t.row['policy_exit']['reason'],'TAKE_PROFIT')
    def test_gaps_and_insufficient_depth_not_zero_returns(self):
        t=Trial(feature(),Policy());t.book(book(100250,size=.001))
        self.assertEqual(t.row['status'],'ENTRY_INSUFFICIENT_DEPTH')
        self.assertIsNone(t.row['forward']['60']['net_return_bps'])
        t=Trial(feature(),Policy());t.expire(103000)
        self.assertEqual(t.row['status'],'ENTRY_QUOTE_MISSING')
    def test_time_stop_and_forward_horizons(self):
        p=Policy(latency_ms=100,fee_bps=0,slippage_bps=0,max_hold_ms=5000,stop_loss_bps=100)
        t=Trial(feature(),p);t.book(book(100100,100,100))
        for ts in range(101100,402100,1000):t.book(book(ts,100,100))
        self.assertEqual(t.row['policy_exit']['reason'],'TIME_LIMIT')
        self.assertEqual(t.row['forward']['60']['net_return_bps'],0)
        self.assertEqual(t.row['status'],'OBSERVED')
    def test_partial_later_missing_preserves_already_observed_horizon(self):
        t=Trial(feature(),Policy(fee_bps=0,slippage_bps=0,latency_ms=0))
        t.book(book(100000,100,100))
        for ts in range(101000,111000,1000):t.book(book(ts,100,100))
        t.expire(120000)
        self.assertEqual(t.row['forward']['10']['status'],'COMPLETE')
        self.assertEqual(t.row['forward']['60']['status'],'FORWARD_FEED_GAP')
    def test_policy_rejects_nan_negative_and_overlapping_clusters(self):
        for kwargs in ({'fee_bps':float('nan')},{'latency_ms':-1},{'cooldown_ms':1},{'notional_krw':0}):
            with self.assertRaises(ValueError):Policy(**kwargs)


class ReplayTests(unittest.TestCase):
    def test_universe_receipt_order_and_gap_checks(self):
        lab=Lab()
        with self.assertRaises(ValueError):lab.feed(trade(1))
        lab=Lab();lab.feed(universe('upbit',[{'market':'KRW-TEST'}],1));lab.feed(trade(2))
        with self.assertRaises(ValueError):lab.feed(trade(1))
    def test_end_to_end_new_coin_detected_without_wave_and_report_not_validated(self):
        lab=Lab(Policy(fee_bps=0,slippage_bps=0));lab.feed(universe('upbit',[{'market':'KRW-TEST'}],1))
        for sec in range(1,91):
            price=100 if sec<72 else min(101,100+(sec-71)*.1)
            lab.feed(book(sec*1000,price,price+.01));lab.feed(trade(sec,price,5 if sec>=72 else 1))
        report=lab.finish()
        self.assertGreater(len(report['trials']),0)
        self.assertEqual(report['profitability_verdict'],'NOT_VALIDATED')
        self.assertEqual(report['mode'],'OFFLINE_REPLAY')
        self.assertEqual(report['groups'][0]['outcomes']['60']['valid'],0)
        self.assertIsNone(report['groups'][0]['outcomes']['60']['mean_net_bps'])
        self.assertGreater(report['counts'].get('CLUSTER_SUPPRESSED',0),0)
    def test_negative_return_never_candidate_even_with_large_flow(self):
        f=feature();f['return_10s_bps']=-1;f['volume_ratio']=100
        self.assertEqual(candidate_reason(f,Policy()),'PRICE')

if __name__=='__main__':unittest.main()
