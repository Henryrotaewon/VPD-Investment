import os
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
import jwt
from magi3.accounts import collect_accounts,normalise,render_accounts
from magi3.adapters.base import CredentialsNotReady
from magi3.adapters.kraken import sign,KrakenAdapter
from magi3.adapters.bithumb import BithumbAdapter
from magi3.adapters.upbit import UpbitAdapter
from magi3.adapters.binance import BinanceAdapter


class AccountsTests(unittest.TestCase):
    def adapters(self):
        a={x:Mock() for x in ('upbit','binance','kraken','bithumb')}
        a['upbit'].accounts.return_value=[{'currency':'KRW','balance':'1000','locked':'100'},
            {'currency':'BTC','balance':'1','locked':'0.5','avg_buy_price':'90','unit_currency':'KRW'}]
        a['bithumb'].accounts.return_value=[]
        a['binance'].account.return_value={'balances':[{'asset':'BTC','free':'1','locked':'0'}]}
        a['kraken'].balance.return_value={'XXBT':{'balance':'1','hold_trade':'0.2'}}
        a['kraken'].assets.return_value={'XXBT':{'altname':'XBT'}}
        return a
    def market(self):
        m=Mock();m.mark.side_effect=lambda venue,asset:{'price_krw':1 if asset=='KRW' else 100,
             'source':'test','received_ts_ms':time.time_ns()//1000000}
        return m
    def test_complete_locked_cost_and_unknown_basis(self):
        r=collect_accounts(self.adapters(),self.market())
        self.assertTrue(r['complete']);self.assertEqual(r['total_asset_krw'],1450)
        self.assertEqual(r['venues'][0]['unrealized_pnl_krw'],15)
        self.assertIsNone(r['venues'][1]['unrealized_pnl_krw'])
        self.assertEqual(r['venues'][2]['positions'][0]['asset'],'BTC')
        self.assertEqual(r['venues'][2]['positions'][0]['pick_basis'],['UNATTRIBUTED'])
        self.assertIsNone(r['realized_pnl_krw'])
    def test_failed_venue_and_unpriced_are_not_zero(self):
        a=self.adapters();a['bithumb'].accounts.side_effect=CredentialsNotReady()
        m=self.market()
        def mark(venue,asset):
            if venue=='binance':raise RuntimeError('no price')
            return {'price_krw':1,'source':'test','received_ts_ms':time.time_ns()//1000000}
        m.mark.side_effect=mark;r=collect_accounts(a,m)
        self.assertFalse(r['complete']);self.assertIsNone(r['total_asset_krw'])
        self.assertEqual(r['venues'][1]['status'],'PARTIAL_VALUATION')
        self.assertIsNone(r['venues'][1]['positions'][0]['value_krw'])
        self.assertIn('확인된 부분합',render_accounts(r))
    def test_invalid_account_values_rejected(self):
        for value in ('nan','inf','-1'):
            with self.assertRaises(ValueError):normalise('binance',{'balances':[{'asset':'BTC','free':value,'locked':'0'}]})
        with self.assertRaises(ValueError):normalise('kraken',{'XXBT':{'balance':'1','credit_used':'2'}})
    def test_placeholder_keys_never_make_network_requests(self):
        http=Mock()
        env={key:'1' for key in ('UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY','BITHUMB_ACCESS_KEY','BITHUMB_SECRET_KEY','BINANCE_API_KEY','BINANCE_SECRET_KEY','KRAKEN_API_KEY','KRAKEN_PRIVATE_KEY')}
        with patch.dict(os.environ,env):
            for method in (UpbitAdapter(session=http).accounts,BithumbAdapter(http).accounts,
                           BinanceAdapter(http).account,KrakenAdapter(http).balance):
                with self.assertRaises(CredentialsNotReady):method()
        http.get.assert_not_called();http.post.assert_not_called()
    def test_bithumb_signed_balance_only(self):
        http=Mock();http.get.return_value.json.return_value=[]
        with patch.dict(os.environ,{'BITHUMB_ACCESS_KEY':'testkey','BITHUMB_SECRET_KEY':'s'*32}):
            BithumbAdapter(http).accounts()
        self.assertTrue(http.get.call_args.args[0].endswith('/v1/accounts'))
        token=http.get.call_args.kwargs['headers']['Authorization'].split()[1]
        p=jwt.decode(token,'s'*32,algorithms=['HS256'])
        self.assertEqual(p['access_key'],'testkey');self.assertIsInstance(p['timestamp'],int)
        self.assertIn('nonce',p);http.post.assert_not_called()
    def test_kraken_official_signature_vector(self):
        secret='kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=='
        data={'nonce':'1616492376594','ordertype':'limit','pair':'XBTUSD','price':37500,'type':'buy','volume':1.25}
        self.assertEqual(sign('/0/private/AddOrder',data,secret),'4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==')
