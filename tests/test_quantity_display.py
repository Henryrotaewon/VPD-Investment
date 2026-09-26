import copy
from decimal import Decimal
import unittest
from magi3.accounts import quantity, render_accounts
from magi2.paper_engine import portfolio_status


class QuantityDisplayTests(unittest.TestCase):
    def test_truncation_and_tiny_holdings(self):
        for value,expected in [(1234.99,'1,234'),(1.99,'1'),(.999,'<1'),(.00000001,'<1'),(0,'0'),
                               (Decimal('9999999999999999.999'),'9,999,999,999,999,999')]:
            self.assertEqual(quantity(value),expected)
    def test_paper_report_only_changes_display(self):
        st=dict(cash_krw=0,initial_cash_krw=1000,positions={'ABC':dict(market='KRW-ABC',qty=9.99,
            entry_price=100,entry_market_price=100,cost_krw=999)})
        before=copy.deepcopy(st);text=portfolio_status(st,{'KRW-ABC':200})
        self.assertIn('ABC | 9개',text)
        self.assertIn('평가 1,997원',text)
        self.assertEqual(st,before)
    def test_live_report_retains_fractional_valuation(self):
        p=dict(asset='BTC',qty=.9,value_krw=900,cost_krw=450,pick_basis=['VPD'])
        report=dict(generated_ts_ms=1000,venues=[dict(venue='upbit',status='OK',valued_total_krw=900,positions=[p])])
        before=copy.deepcopy(report);text=render_accounts(report,1000)
        self.assertIn('BTC <1개',text);self.assertIn('900원',text);self.assertIn('+100.00%',text)
        self.assertEqual(report,before)
