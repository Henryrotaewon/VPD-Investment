import unittest
from decimal import Decimal
from scanner.fast_case_study import ClosedBars, spread_floor_bps, move_ticks, sensitivity, price_probes
from magi2.fast_captures import strength


def bar(ts, price, close=None):
    close = price if close is None else close
    return dict(ts=ts,open=price,close=close,high=max(price,close),low=min(price,close),quote_volume=1)


class CaseStudyTests(unittest.TestCase):
    def test_future_bar_close_cannot_enter_signal(self):
        rows=[bar(0,7,7.1),bar(60000,7.1,9)]
        b=ClosedBars(rows,60000)
        self.assertIsNone(b.at(59999))
        self.assertEqual(b.at(60000)['close'],7.1)
        self.assertEqual(b.at(119999)['close'],7.1)
        self.assertEqual(b.at(120000)['close'],9)

    def test_tick_boundary_and_production_strength_exclusion(self):
        self.assertEqual(move_ticks(10,-1,1),Decimal('9.99'))
        self.assertEqual(move_ticks(9.99,1,1),Decimal('10.00'))
        self.assertAlmostEqual(spread_floor_bps(10),100)
        for p in (6.29,6.66,7.31,9.99,10,12,14):
            floor=spread_floor_bps(p)
            self.assertGreater(floor,10)
            # Even perfect measured buying does not overcome the current spread gate.
            self.assertFalse(strength({'sufficient':True,'buyer_share_pct':100},
                                     {'spread_bps':floor,'breakout_bps':50})['strong'])

    def test_delayed_entry_uses_future_open_not_signal_close(self):
        rows=[bar(1000,7),bar(2000,7.1),bar(3000,7.2),bar(62000,7.5)]
        r=sensitivity(rows,1000,1000,60000,1,5)
        self.assertEqual(r['entry_ms'],2000)
        self.assertEqual(r['assumed_buy'],7.11)
        self.assertEqual(r['assumed_sell'],7.49)
        self.assertFalse(r['execution_eligible'])
        self.assertGreater(r['net_bps'],0)

    def test_missing_future_is_unavailable_not_a_win(self):
        r=sensitivity([bar(1000,7),bar(2000,7.1)],1000,1000,60000)
        self.assertEqual(r['status'],'MISSING_TRADE_BAR')
        self.assertIsNone(r['net_bps'])

    def test_sparse_seconds_do_not_fabricate_breakout_history(self):
        rows=[bar(0,7),bar(29000,7),bar(30000,7.01)]
        out=price_probes(rows,[dict(ts=0,price=7,price_gate_pass=True)])
        self.assertIsNone(out[0]['probe'])
        self.assertEqual(out[0]['full_fast_signal'],'NOT_ESTABLISHED')

    def test_duplicate_bars_rejected(self):
        with self.assertRaises(ValueError):
            ClosedBars([bar(0,7),bar(0,7)],1000)


if __name__=='__main__':
    unittest.main()
