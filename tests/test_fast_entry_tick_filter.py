import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from magi2.fast_rank_paper import RankLedger, rank_day, INTERVAL
from magi2.fast_rank_monitor import RankPaperService
from magi2.fast_tick_rules import UPBIT_GRID, BITHUMB_GRID
from magi2.fast_paper_report import positions_page, menu

START = rank_day(1789970000000) + 60000


def book(ts, ask, bid=None):
    return dict(requested_ms=ts-20, received_ms=ts,
                bids=[(ask*.9 if bid is None else bid, 1e12)], asks=[(ask, 1e12)])


def rules(**price_rule):
    return dict(step='0.00000001', min_qty=0, min_notional=1,
                source='TEST_EXCHANGE_RULES', **price_rule)


class EntryTickFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.db'
        self.l = RankLedger(self.path, START)
        for venue in ('binance', 'kraken'):
            self.l.fund(venue, 1400, 'test', START)

    def tearDown(self):
        self.l.close()
        self.tmp.cleanup()

    def offer(self, venue, symbol, ts=START):
        row = dict(symbol=symbol, asset=symbol, price=100., rise_pct=25., rank=1)
        self.assertTrue(self.l.save_rank(venue, ts, [row], ts, self.l.control()['generation']))
        return self.l.offer_rank(venue, ts)[0]

    def test_btt_rejected_before_fill_with_cash_slot_and_evidence_preserved(self):
        ident = self.offer('bithumb', 'KRW-BTT')
        before = self.l.account('bithumb')['cash_quote']
        self.assertFalse(self.l.enter_market(ident, book(START+300, .0005, .0004),
                                            rules(grid=BITHUMB_GRID), START+300))
        t = self.l.get(ident)
        self.assertEqual((t['status'], t['reason']), ('SKIPPED', 'ENTRY_TICK_TOO_LARGE'))
        self.assertEqual(t['entry_tick_pct'], 20.)
        self.assertEqual(t['entry_tick_price'], .0005)
        self.assertEqual(t['entry_tick_size'], .0001)
        self.assertEqual(t['entry_tick_limit_pct'], 6)
        self.assertEqual(t['session_cash'], 0)
        self.assertEqual(t['entry_cost'], 0)
        self.assertEqual(t['remaining_qty'], 0)
        self.assertIsNone(t['order'])
        self.assertEqual(self.l.account('bithumb')['cash_quote'], before)
        self.assertEqual(self.l.active('bithumb'), [])
        self.assertEqual(self.l.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0], 0)
        evidence = json.loads(self.l.db.execute(
            "SELECT payload FROM tick_order_events WHERE event='ENTRY_BLOCKED'").fetchone()[0])
        self.assertEqual(evidence['entry_tick_pct'], 20.)
        text, _ = positions_page(self.l, START+300)
        self.assertIn('1틱 20.00% · 6% 이상 매수 제외', text)
        self.assertIn('판정 가격 0.0005원 / 호가 단위 0.0001원', text)
        self.assertIn('1틱 6% 이상 매수 제외', menu(self.l)[0])
        self.l.close()
        self.l = RankLedger(self.path, START+1000)
        self.assertEqual(self.l.get(ident)['reason'], 'ENTRY_TICK_TOO_LARGE')
        self.assertEqual(self.l.account('bithumb')['cash_quote'], before)
        # A new eligible TOP5 member can use the released slot/cash.
        other = self.offer('bithumb', 'KRW-NEXT', START+INTERVAL)
        self.assertEqual(self.l.get(other)['budget_quote'], 200000)

    def test_exact_six_is_rejected_without_slippage_diluting_boundary(self):
        ident = self.offer('binance', 'BOUNDARYUSDT')
        self.assertFalse(self.l.enter_market(ident, book(START+300, 1), rules(tick='.06'), START+300))
        self.assertEqual(self.l.get(ident)['entry_tick_pct'], 6.)
        self.assertEqual(self.l.get(ident)['reason'], 'ENTRY_TICK_TOO_LARGE')

    def test_less_than_six_including_five_is_allowed(self):
        for venue, tick in [('binance', '.05'), ('kraken', '.059999999')]:
            with self.subTest(venue=venue):
                ident = self.offer(venue, 'ALLOWED')
                self.assertTrue(self.l.enter_market(ident, book(START+300, 1), rules(tick=tick), START+300))
                self.assertEqual(self.l.get(ident)['status'], 'OPEN')
                self.assertLess(self.l.get(ident)['entry_tick_pct'], 6)

    def test_all_four_venues_use_their_price_rules(self):
        cases = [('bithumb', .0005, rules(grid=BITHUMB_GRID)),
                 ('upbit', .0000001, rules(grid=UPBIT_GRID)),
                 ('binance', .1, rules(tick='.01')),
                 ('kraken', .1, rules(tick='.01'))]
        for venue, price, price_rules in cases:
            with self.subTest(venue=venue):
                ident = self.offer(venue, 'COARSE')
                self.assertFalse(self.l.enter_market(ident, book(START+300, price), price_rules, START+300))
                self.assertEqual(self.l.get(ident)['reason'], 'ENTRY_TICK_TOO_LARGE')

    def test_stale_book_does_not_produce_an_exclusion_or_a_fill(self):
        ident = self.offer('bithumb', 'KRW-BTT')
        with self.assertRaisesRegex(ValueError, 'STALE_OR_EARLY_BOOK'):
            self.l.enter_market(ident, book(START+300, .0005), rules(grid=BITHUMB_GRID), START+4000)
        self.assertEqual(self.l.get(ident)['status'], 'ENTRY_PENDING')

    def test_existing_holdings_still_follow_rank_and_stop(self):
        ident = self.offer('upbit', 'HELD')
        self.assertTrue(self.l.enter_market(ident, book(START+300, 100, 99), rules(tick='1'), START+300))
        cash = self.l.account('upbit')['cash_quote']
        # The filter belongs only to entry; it cannot remove an existing holding.
        self.assertFalse(self.l.enter_market(ident, book(START+1000, .0005), rules(grid=BITHUMB_GRID), START+1000))
        self.assertEqual(self.l.get(ident)['status'], 'OPEN')
        self.assertEqual(self.l.account('upbit')['cash_quote'], cash)
        self.offer('upbit', 'NEW', START+INTERVAL)
        b = book(START+INTERVAL+300, 95, 90)
        self.assertTrue(self.l.check_stop(ident, b, START+INTERVAL+300))
        self.assertTrue(self.l.exit(ident, b, START+INTERVAL+300))
        self.assertEqual(self.l.get(ident)['status'], 'CLOSED')
        self.assertEqual(self.l.get(ident)['reason'], 'TOP5_EXIT_AND_STOP_6')

    def test_worker_records_tick_exclusion_as_transition_not_error(self):
        ident = self.offer('bithumb', 'KRW-BTT')
        service = RankPaperService.__new__(RankPaperService)
        service.ledger = self.l
        service.clock = lambda: START+300
        service.log = Mock()
        market = Mock()
        market.book.return_value = book(START+300, .0005, .0004)
        market.rules.return_value = rules(grid=BITHUMB_GRID)
        service.step('bithumb', market)
        self.assertEqual(self.l.get(ident)['reason'], 'ENTRY_TICK_TOO_LARGE')
        messages = [call.args[0] for call in service.log.call_args_list]
        self.assertTrue(any('"entry_tick_pct":20.0' in line for line in messages))
        self.assertFalse(any('fast_target_error' in line for line in messages))


if __name__ == '__main__':
    unittest.main()
