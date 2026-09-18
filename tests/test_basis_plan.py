import unittest
from decimal import Decimal
from unittest.mock import patch
from magi2.basis_plan import paired_pnl, hedge_action, assess_basis_setup, basis_text, stressed_funding_receipt
from magi2 import server_runner as server


def snapshot():
    return dict(asset='BTC',spot_venue='binance',future_venue='binance',
                spot_quote='USDT',future_quote='USDT',settlement_currency='USDT',
                linear_contract=True,contract_verified=True,borrowed=False,
                fees_verified=True,costs_complete=True,margin_data_verified=True,
                spot_quote_ts_ms=10000,future_quote_ts_ms=10000,base_qty=1,
                spot_ask=100,future_bid=102,short_base_qty=1,rounded_hedge_verified=True,
                initial_margin=102,cash_reserve=21,free_capital_covers_both_legs=True,
                scenario_asof_ms=10000,stressed_net_return_on_capital_bps=60,
                contract_type='DATED',days_to_expiry=30,settlement_alignment_verified=True)


class BasisPlanTests(unittest.TestCase):
    def test_funding_haircut_does_not_discount_payments_owed(self):
        self.assertEqual(Decimal(stressed_funding_receipt(1000,.001,.0005)),Decimal('.25'))
        self.assertEqual(Decimal(stressed_funding_receipt(1000,-.001,.0005)),Decimal('-1'))

    def test_pnl_cancels_direction_and_accounts_for_four_fees_and_all_capital(self):
        args=dict(base_qty=1,spot_entry=100,future_entry=102,spot_exit=110,future_exit=110,
                  spot_entry_fee_bps=10,future_entry_fee_bps=5,spot_exit_fee_bps=10,
                  future_exit_fee_bps=5,funding_received=0,other_costs=0.2,
                  initial_margin=102,cash_reserve=21)
        r=paired_pnl(**args)
        self.assertEqual(Decimal(r['spot_pnl']),Decimal(10))
        self.assertEqual(Decimal(r['future_pnl']),Decimal(-8))
        self.assertEqual(Decimal(r['fees']),Decimal('.316'))
        self.assertEqual(Decimal(r['net_pnl']),Decimal('1.484'))
        self.assertEqual(Decimal(r['committed_capital']),Decimal('223.151'))
        paid=paired_pnl(**dict(args,funding_received=-1))
        self.assertEqual(Decimal(paid['net_pnl']),Decimal('.484'))

    def test_widening_basis_can_lose_despite_positive_funding(self):
        r=paired_pnl(base_qty=1,spot_entry=100,future_entry=101,spot_exit=110,future_exit=113,
                     spot_entry_fee_bps=0,future_entry_fee_bps=0,spot_exit_fee_bps=0,
                     future_exit_fee_bps=0,funding_received=.5,other_costs=0,
                     initial_margin=101,cash_reserve=21)
        self.assertLess(Decimal(r['net_pnl']),0)

    def test_missing_leg_unknown_timeout_and_quantity_hedge(self):
        self.assertEqual(hedge_action(1,1,0),'HEDGED')
        self.assertEqual(hedge_action(1,0,1000),'HEDGE_PENDING')
        self.assertEqual(hedge_action(1,0,2000),'UNWIND_EXCESS_FILLED_LEG')
        self.assertEqual(hedge_action(1,0,2000,False),'RECONCILE_BOTH_LEGS')
        self.assertEqual(hedge_action(0,0,0),'NO_POSITION')
        self.assertEqual(hedge_action(float('nan'),1,0),'RECONCILE_BOTH_LEGS')

    def test_entry_is_research_only_and_rejects_fx_or_margin_or_stale(self):
        r=assess_basis_setup(snapshot(),10000)
        self.assertEqual(r['decision'],'RESEARCH_CANDIDATE')
        self.assertEqual(r['allocated_real_capital_pct'],0)
        self.assertFalse(r['live_eligible'])
        for fields in ({'spot_venue':'upbit'}, {'spot_quote':'KRW'}, {'linear_contract':False},
                       {'initial_margin':51}, {'cash_reserve':0}, {'spot_quote_ts_ms':9000},
                       {'future_quote_ts_ms':10001}, {'short_base_qty':.9}, {'borrowed':True},
                       {'stressed_net_return_on_capital_bps':10}, {'contract_verified':False}):
            self.assertEqual(assess_basis_setup(dict(snapshot(),**fields),10000)['decision'],'BLOCKED')
        self.assertEqual(assess_basis_setup({},10000)['decision'],'BLOCKED')

    def test_perpetual_cannot_extrapolate_just_the_latest_funding(self):
        s=dict(snapshot(),contract_type='PERPETUAL',funding_history_days=7,
               positive_funding_fraction=.8,funding_schedule_verified=True,
               conservative_funding_scenario=True,scenario_horizon_hours=24,next_funding_rate=.0001)
        self.assertEqual(assess_basis_setup(s,10000)['decision'],'RESEARCH_CANDIDATE')
        s['conservative_funding_scenario']=False
        self.assertIn('FUNDING_SCENARIO_REQUIRED',assess_basis_setup(s,10000)['reasons'])
        s['next_funding_rate']=-.0001
        self.assertIn('NEGATIVE_FUNDING',assess_basis_setup(s,10000)['reasons'])

    def test_guide_callback_cannot_start_any_trading(self):
        with patch.object(server,'ALLOWED_CHAT_ID','7'), patch.object(server,'telegram_api'), \
             patch.object(server,'telegram') as send, patch.object(server,'start_engine') as start:
            server.handle_callback({'id':'basis','data':'guide:basis','from':{'id':'7'},
                                    'message':{'chat':{'id':'7'}}})
            self.assertIn('현물·선물',send.call_args.args[0])
            start.assert_not_called()
        self.assertLess(len(basis_text().encode('utf-16-le'))//2,4096)


if __name__=='__main__':
    unittest.main()
