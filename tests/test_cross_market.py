import copy
import unittest
from decimal import Decimal
from unittest.mock import patch
from magi2.cross_market import instrument_key, entry_gap, funding_cashflow, guide_text
from magi2 import server_runner as server


def leg(venue='a',kind='SPOT',quote='USDT',bid=99,ask=100):
    return dict(venue=venue,asset_id='bitcoin',kind=kind,symbol='BTC-'+quote,
                quote=quote,settlement=quote,expiry_ms=None,metadata_verified=True,
                depth_complete=True,linear=True,quote_ts_ms=10000,base_qty=1,
                bid_vwap=bid,ask_vwap=ask)


class CrossMarketTests(unittest.TestCase):
    def test_contract_identity_does_not_merge_venue_spot_perp_and_expiries(self):
        a=leg();b=leg(kind='PERPETUAL')
        self.assertNotEqual(instrument_key(a),instrument_key(b))
        c=dict(leg(kind='DATED'),expiry_ms=20000)
        self.assertEqual(instrument_key(c),instrument_key(dict(c,expiry_ms='2e4')))
        self.assertNotEqual(instrument_key(c),instrument_key(dict(c,expiry_ms=30000)))
        with self.assertRaises(ValueError):instrument_key(leg(kind='DATED'))

    def test_gap_is_quantity_matched_observation_not_profit_or_permission(self):
        a,b=leg(),leg('b','PERPETUAL',bid=102,ask=103)
        before=copy.deepcopy((a,b))
        r=entry_gap(a,b,'USDT',10000)
        self.assertEqual(r['gross_entry_gap_bps'],200)
        self.assertIsNone(r['expected_net_profit']);self.assertFalse(r['execution_eligible'])
        self.assertEqual((a,b),before)
        self.assertEqual(entry_gap(a,dict(b,base_qty=2),'USDT',10000)['status'],'UNAVAILABLE')

    def test_fx_has_direction_timestamp_and_no_stablecoin_parity_assumption(self):
        a=leg(quote='KRW',bid=99000,ask=100000)
        b=leg('b','PERPETUAL',bid=100,ask=101)
        r=entry_gap(a,b,'KRW',10000)
        self.assertEqual(r['status'],'UNAVAILABLE')
        b['fx']={'from':'USDT','to':'KRW','bid':990,'ask':1010,'ts_ms':10000,'verified':True}
        r=entry_gap(a,b,'KRW',10000)
        self.assertEqual(r['gross_entry_gap_bps'],-100)
        self.assertTrue(r['currency_hedge_required'])
        b['fx']['ts_ms']=8000
        self.assertEqual(entry_gap(a,b,'KRW',10000)['status'],'UNAVAILABLE')
        self.assertEqual(entry_gap(leg(quote='USD'),leg('b'),'USD',10000)['status'],'UNAVAILABLE')

    def test_rejects_stale_asynchronous_nonfinite_or_different_asset_quotes(self):
        for p in ({'quote_ts_ms':8000},{'quote_ts_ms':10001},{'quote_ts_ms':9400},
                  {'asset_id':'wrapped-bitcoin'},{'bid_vwap':float('nan')},
                  {'depth_complete':False},{'linear':False}):
            self.assertEqual(entry_gap(leg(),dict(leg('b','PERPETUAL'),**p),'USDT',10000)['status'],'UNAVAILABLE')

    def funding_legs(self):
        a=dict(leg('a','PERPETUAL'),side='LONG',schedule_complete=True,coverage_start_ms=0,coverage_end_ms=8000,
               events=[dict(ts_ms=i*1000,notional_quote=1000,rate=.0001,kind='SCENARIO') for i in range(1,9)])
        b=dict(leg('b','PERPETUAL'),side='SHORT',schedule_complete=True,coverage_start_ms=0,coverage_end_ms=8000,
               events=[dict(ts_ms=8000,notional_quote=1000,rate=.0005,kind='SCENARIO')])
        return [a,b]

    def test_higher_nominal_rate_can_pay_less_over_equal_time_window(self):
        r=funding_cashflow(self.funding_legs(),0,8000)
        self.assertEqual(Decimal(r['net_funding_quote']),Decimal('-.3'))
        self.assertEqual(r['events'],9);self.assertFalse(r['execution_eligible'])

    def test_incomplete_duplicate_and_actual_scenario_mixing_never_mean_zero(self):
        legs=self.funding_legs();legs[0]['schedule_complete']=False
        self.assertIsNone(funding_cashflow(legs,0,8000)['net_funding_quote'])
        legs=self.funding_legs();legs[0]['events'].append(legs[0]['events'][0])
        self.assertEqual(funding_cashflow(legs,0,8000)['status'],'UNAVAILABLE')
        self.assertEqual(funding_cashflow(self.funding_legs(),0,8000,'ACTUAL')['status'],'UNAVAILABLE')
        legs=self.funding_legs()
        for l in legs:
            for e in l['events']:e.update(kind='ACTUAL',settled=True,cashflow_quote=-.1 if l['side']=='LONG' else .499)
        self.assertEqual(Decimal(funding_cashflow(legs,0,8000,'ACTUAL')['net_funding_quote']),Decimal('-.301'))

    def test_read_only_button_and_telegram_length(self):
        with patch.object(server,'ALLOWED_CHAT_ID','7'),patch.object(server,'telegram_api'),\
             patch.object(server,'telegram') as send,patch.object(server,'start_engine') as start:
            server.handle_callback({'id':'cross','data':'guide:cross','from':{'id':'7'},'message':{'chat':{'id':'7'}}})
            self.assertIn('통합 연구',send.call_args.args[0]);start.assert_not_called()
        self.assertLess(len(guide_text().encode('utf-16-le'))//2,4096)
