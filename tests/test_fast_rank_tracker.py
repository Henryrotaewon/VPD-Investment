import unittest
from dataclasses import replace
from magi2.fast_lab.rank_tracker import RankTracker, RankPolicy, PaperScalper, ScalpPolicy


class RankingTests(unittest.TestCase):
    def test_short_window_ranks_not_24h_and_no_future_baseline(self):
        tracker=RankTracker(RankPolicy(scan_minutes=10,top_n=2,min_rise_bps=50))
        tracker.snapshot('upbit',0,{'A':100,'B':100,'C':100})
        self.assertEqual(tracker.scan('upbit',0)['status'],'WARMING_UP')
        tracker.snapshot('upbit',600000,{'A':103,'B':101,'C':99})
        report=tracker.scan('upbit',600000)
        self.assertEqual([r['symbol'] for r in report['watchlist']],['A','B'])
        self.assertIsNone(report['watchlist'][0]['returns_bps']['15'])
        self.assertIsNone(report['watchlist'][0]['rank_jump'])
        self.assertIsNone(tracker.scan('upbit',600001))
        tracker.snapshot('upbit',1200000,{'A':104,'B':105,'C':100})
        report=tracker.scan('upbit',1200000)
        self.assertEqual(report['watchlist'][0]['symbol'],'B')
        self.assertEqual(report['watchlist'][0]['rank_jump'],1)

    def test_missing_history_and_other_venue_do_not_invent_returns(self):
        tracker=RankTracker()
        tracker.snapshot('upbit',0,{'A':100})
        tracker.snapshot('binance',0,{'B':100})
        tracker.snapshot('upbit',950000,{'A':105,'NEW':100})
        rows=tracker.scan('upbit',950000)
        self.assertEqual([r['symbol'] for r in rows['watchlist']],['A'])
        tracker.snapshot('upbit',3000000,{'A':120})
        self.assertEqual(tracker.scan('upbit',3000000)['status'],'WARMING_UP')
        with self.assertRaises(ValueError):tracker.snapshot('upbit',2999999,{'A':1})


class ScalperTests(unittest.TestCase):
    def scalper(self,**changes):
        policy=replace(ScalpPolicy(),lookback_ms=1000,max_gap_ms=2000,**changes)
        return PaperScalper('TEST',300000,10,policy)
    def warm_entry(self,s,base=0):
        s.quote(base,100,100.01,10,10)
        s.quote(base+500,100,100.01,10,10)
        s.quote(base+1000,100.1,100.11,10,10)
        self.assertEqual(s.state,'ENTRY_PENDING')
        s.quote(base+1250,100.1,100.11,10,10)
        self.assertEqual(s.state,'OPEN')
    def test_costs_latency_exit_and_reentry_without_forced_trade(self):
        s=self.scalper(cooldown_ms=500)
        self.warm_entry(s)
        s.quote(1500,100.1,100.11,10,10)
        self.assertEqual(s.state,'OPEN')  # costs alone do not trigger stop
        s.quote(1750,100.7,100.71,10,10)
        self.assertEqual(s.state,'EXIT_PENDING')
        s.quote(2000,100.7,100.71,10,10)
        self.assertEqual(len(s.trades),1)
        self.assertGreater(s.trades[0]['net_bps'],15)
        self.warm_entry(s,2500)
        self.assertEqual(s.budget['entries'],2)
        report=s.finish()
        self.assertEqual(report['exclusions'],['END_OF_OBSERVATION'])
        self.assertEqual(len(report['trades']),1)
    def test_session_trade_cap_shared_and_no_quota(self):
        budget={'entries':15,'net_bps':0.}
        s=PaperScalper('TEST',300000,10,budget=budget)
        s.quote(0,100,100.01,10,10)
        self.assertEqual(s.state,'HALTED')
        s=self.scalper()
        for ts in range(0,10000,500):s.quote(ts,100,100.01,10,10)
        self.assertEqual(s.budget['entries'],0)
    def test_gap_and_insufficient_depth_never_become_profitable_fills(self):
        s=self.scalper();self.warm_entry(s)
        s.quote(5000,110,110.01,10,10)
        self.assertEqual(s.exclusions,['QUOTE_GAP'])
        self.assertEqual(s.trades,[])
        s=self.scalper();self.warm_entry(s)
        s.quote(1500,101,101.01,10,10)
        s.quote(1750,101,101.01,.0001,10)
        self.assertEqual(s.exclusions,['EXIT_UNOBSERVABLE'])
    def test_cost_bound_and_invalid_policy_rejected(self):
        with self.assertRaises(ValueError):ScalpPolicy(stop_net_bps=20)
        with self.assertRaises(ValueError):ScalpPolicy(max_trades_per_session=16)
        with self.assertRaises(ValueError):RankPolicy(scan_minutes=1)
