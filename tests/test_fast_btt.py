import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from magi2.fast_paper import checked_book
from magi2.fast_tick_service import TickMarket
from magi2.fast_target_paper import TargetLedger
from magi2.fast_target_service import TargetPaperService
from test_fast_tick import START


def padded_book(ts):
    # Illustrative low-priced book: one side has fewer levels than the other.
    return dict(requested_ms=ts-20,received_ms=ts,
                bids=[(.0004,1e12),(.0003,1e12),(0,0),(0,0)],
                asks=[(.0005,1e12),(.0006,1e12),(.0007,1e12),(.0008,1e12)])


class BttBookTests(unittest.TestCase):
    def test_padding_ignored_independently_on_each_side(self):
        raw=padded_book(START)
        bids,asks=checked_book(raw,START)
        self.assertEqual(bids,raw['bids'][:2]);self.assertEqual(asks,raw['asks'])
        raw['asks']=[('.0005','1000'),('0','0')]
        self.assertEqual(checked_book(raw,START)[1],[(.0005,1000.)])

    def test_zero_with_quantity_and_other_invalid_prices_are_still_rejected(self):
        for bad in [(0,1),(-1,0),(-1,1),(float('nan'),0),(float('inf'),0)]:
            for side in ('bids','asks'):
                with self.subTest(side=side,bad=bad):
                    raw=padded_book(START);raw[side].append(bad)
                    with self.assertRaisesRegex(ValueError,'INVALID_POSITIVE_NUMBER'):
                        checked_book(raw,START)
        for bad in [(0,-1),(0,float('nan')),(0,float('inf'))]:
            with self.assertRaisesRegex(ValueError,'INVALID_BOOK_SIZE'):
                checked_book(dict(padded_book(START),bids=[bad]),START)
        for side in ('bids','asks'):
            with self.assertRaisesRegex(ValueError,'EMPTY_OR_CROSSED_BOOK'):
                checked_book(dict(padded_book(START),**{side:[(0,0)]}),START)
        with self.assertRaisesRegex(ValueError,'EMPTY_OR_CROSSED_BOOK'):
            checked_book(dict(padded_book(START),bids=[(.001,100),(0,0)]),START)
        with self.assertRaisesRegex(ValueError,'STALE_OR_EARLY_BOOK'):
            checked_book(padded_book(START),START+3001)

    def test_adapter_to_target_entry_and_exit_with_padded_depth(self):
        with tempfile.TemporaryDirectory() as root,patch('magi2.fast_monitor.PublicMarket') as factory:
            ledger=TargetLedger(Path(root)/'paper.db',START)
            self.addCleanup(ledger.close)
            market=TickMarket('bithumb');ts=START+300
            raw=padded_book(ts)
            units=[dict(bid_price=b[0],bid_size=b[1],ask_price=a[0],ask_size=a[1])
                   for b,a in zip(raw['bids'],raw['asks'])]
            factory.return_value.get.return_value=[dict(market='KRW-BTT',orderbook_units=units)]
            self.assertTrue(ledger.offer('btt','bithumb','KRW-BTT',START,START))
            service=TargetPaperService.__new__(TargetPaperService)
            service.ledger=ledger;service.clock=lambda:ts;logs=[];service.log=logs.append
            with patch('magi2.fast_paper_service.now',side_effect=[ts-20,ts]):
                service.step('bithumb',market)
            t=ledger.get('btt')
            self.assertEqual(t['status'],'OPEN');self.assertGreater(t['entry_qty'],0)
            self.assertLessEqual(t['entry_cost'],200000.00001)
            self.assertEqual(t['entry_bid'],.0004);self.assertEqual(t['entry_ask'],.0005)
            self.assertAlmostEqual(ledger.account('bithumb')['cash_quote']+t['entry_cost'],1000000)
            # The valid book also remains usable for a market stop/exit.
            ts=START+1300
            with patch('magi2.fast_paper_service.now',side_effect=[ts-20,ts]):
                service.step('bithumb',market)
            self.assertEqual(ledger.get('btt')['status'],'CLOSED')
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],2)
            self.assertFalse(any('INVALID_POSITIVE_NUMBER' in x for x in logs))

    def test_recheck_is_read_only_and_never_replays_expired_entry(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=TargetLedger(Path(root)/'paper.db',START)
            self.addCleanup(ledger.close)
            ledger.offer('old','bithumb','KRW-BTT',START,START)
            ledger.fail('old','INVALID_POSITIVE_NUMBER');ledger.advance('bithumb',START+10001)
            before=ledger.get('old');changes=ledger.db.total_changes
            service=TargetPaperService.__new__(TargetPaperService)
            service.ledger=ledger;service.clock=lambda:START+11000;logs=[];service.log=logs.append
            market=Mock();market.book.return_value=padded_book(START+11000)
            service.probe_recent_entry_books('bithumb',market)
            self.assertIn('symbol=KRW-BTT status=VALID',logs[0])
            self.assertIn('zero_padding_bids=2',logs[0])
            self.assertEqual(market.method_calls,[('book',('KRW-BTT',),{})])
            self.assertEqual(ledger.get('old'),before)
            self.assertEqual(ledger.db.total_changes,changes)
            self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],0)


if __name__=='__main__':unittest.main()
