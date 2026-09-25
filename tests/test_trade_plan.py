import copy
import unittest
from decimal import Decimal
from magi2.trade_plan import assess_entry, assess_exit, net_return_bps, policy, promotion_review
from magi2.strategy_guide import strategy_text

NOW = 1_000_000


def setup():
    return dict(venue='upbit', market='KRW-BTC', side='BUY', market_active=True,
                quality_ok=True, quote_ts_ms=NOW, decision_ts_ms=NOW,
                bid=100, ask=100.05, chase_bps=30, buyer_share_pct=75,
                buy_fee_bps=5, sell_fee_bps=5, entry_slippage_bps=2,
                exit_slippage_bps=2, latency_buffer_bps=2, fees_verified=True, fee_ts_ms=NOW,
                rise_5m_bps=150, rank=2, rank_jump=5, lookback_ms=31000,
                breakout_bps=12, sample_trades=30, sample_span_ms=6000,
                latest_trade_age_ms=500, watch_started_ms=NOW-40000,
                ask_depth_krw=1_000_000, bid_depth_krw=1_000_000,
                min_order_krw=5000, signal_consumed=False,
                fresh_setup_after_exit=False)


def capital():
    return dict(complete=True, observed_ts_ms=NOW, asset_busy=False,
                unresolved_orders=0, positions=0, strategy_positions=0,
                entries_today=0, episode_entries=0, pause_until_ms=0,
                consecutive_losses=0, last_exit_ts_ms=None, equity_krw=3_000_000,
                free_cash_krw=100_000, shared_exposure_krw=0, daily_pnl_krw=0,
                day_start_equity_krw=3_000_000)


def wave():
    return dict(setup(), source_contract='wave-confirmed-setup-v1', independent_triggers=True,
                timing_resolved=True, market_residual_positive=True,
                confirming_venues=['binance', 'bybit'], origin_ts_ms=NOW-5000,
                confirmed_ts_ms=NOW-1000, origin_return_z=3.5, origin_volume_z=2.5,
                destination_return_15s_bps=15)


class TradePlanTests(unittest.TestCase):
    def test_candidate_caps_fees_depth_and_cash_without_mutation(self):
        s, c = setup(), capital()
        before = copy.deepcopy((s, c))
        result = assess_entry('FAST', s, c, NOW)
        self.assertEqual(result['decision'], 'PAPER_CANDIDATE')
        self.assertFalse(result['live_eligible'])
        self.assertFalse(result['order_submitted'])
        self.assertLessEqual(Decimal(result['notional_krw'])*Decimal('1.0005'), 30000)
        self.assertEqual((s, c), before)
        s['bid_depth_krw'] = 60000
        self.assertEqual(assess_entry('FAST', s, c, NOW)['notional_krw'], '6000')
        c['free_cash_krw'] = 4999
        self.assertEqual(assess_entry('FAST', s, c, NOW)['decision'], 'BLOCKED')

    def test_joint_exposure_and_fees_enforce_strict_under_five_percent(self):
        c = dict(capital(), shared_exposure_krw=145000)
        r = assess_entry('FAST', setup(), c, NOW)
        self.assertIn('MINIMUM_OR_BUDGET', r['reasons'])
        c['shared_exposure_krw'] = 140000
        r = assess_entry('FAST', setup(), c, NOW)
        self.assertEqual(r['decision'], 'PAPER_CANDIDATE')
        self.assertLess(Decimal(r['notional_krw'])*Decimal('1.0005')+140000, 150000)

    def test_incomplete_stale_future_and_unknown_inputs_fail_closed(self):
        for patch in ({'complete':False}, {'observed_ts_ms':NOW-5001},
                      {'observed_ts_ms':NOW+1}, {'equity_krw':float('nan')},
                      {'unresolved_orders':1}, {'asset_busy':True}, {'positions':-1}, {'positions':False}):
            self.assertEqual(assess_entry('FAST', setup(), dict(capital(), **patch), NOW)['decision'], 'BLOCKED')
        for patch in ({'fees_verified':False}, {'fee_ts_ms':NOW-300001}, {'market':None}, {'quote_ts_ms':NOW+1},
                      {'quote_ts_ms':NOW-1501}, {'ask':float('inf')},
                      {'entry_slippage_bps':-1}, {'bid_depth_krw':0}):
            self.assertEqual(assess_entry('FAST', dict(setup(), **patch), capital(), NOW)['decision'], 'BLOCKED')
        self.assertEqual(assess_entry('FAST', {}, {}, NOW)['decision'], 'BLOCKED')

    def test_recent_rise_is_not_expected_profit_or_reason_to_ignore_costs(self):
        s = dict(setup(), rise_5m_bps=500, entry_slippage_bps=20)
        self.assertIn('COST_LIMIT', assess_entry('FAST', s, capital(), NOW)['reasons'])
        s = dict(setup(), chase_bps=101)
        self.assertIn('CHASE_LIMIT', assess_entry('FAST', s, capital(), NOW)['reasons'])

    def test_reentry_needs_new_signal_cooldown_and_episode_capacity(self):
        c = dict(capital(), last_exit_ts_ms=NOW-61000, episode_entries=1)
        self.assertIn('NEW_SETUP_REQUIRED', assess_entry('FAST', setup(), c, NOW)['reasons'])
        s = dict(setup(), fresh_setup_after_exit=True)
        self.assertEqual(assess_entry('FAST', s, c, NOW)['decision'], 'PAPER_CANDIDATE')
        c['last_exit_ts_ms'] = NOW-59000
        self.assertIn('REENTRY_COOLDOWN', assess_entry('FAST', s, c, NOW)['reasons'])
        c['episode_entries'] = 3
        self.assertIn('EPISODE_ENTRY_LIMIT', assess_entry('FAST', s, c, NOW)['reasons'])

    def test_limits_are_entry_only_and_do_not_force_trade_count(self):
        for field, value, reason in [('entries_today',15,'DAILY_ENTRY_LIMIT'),
                                     ('consecutive_losses',3,'LOSS_STREAK'),
                                     ('daily_pnl_krw',-3000,'DAILY_LOSS_LIMIT'),
                                     ('positions',3,'POSITION_LIMIT')]:
            self.assertIn(reason, assess_entry('FAST', setup(), dict(capital(), **{field:value}), NOW)['reasons'])

    def test_wave_needs_independent_non_target_confirmation_after_origin(self):
        self.assertEqual(assess_entry('WAVE', wave(), capital(), NOW)['decision'], 'PAPER_CANDIDATE')
        for patch in ({'confirming_venues':['binance','binance']},
                      {'confirming_venues':['binance','upbit']},
                      {'source_contract':'wave-timeshift-catalog-v1'},
                      {'independent_triggers':False}, {'timing_resolved':False},
                      {'market_residual_positive':False}, {'confirmed_ts_ms':NOW-4000},
                      {'origin_ts_ms':NOW}, {'origin_return_z':2.9}):
            self.assertEqual(assess_entry('WAVE', dict(wave(), **patch), capital(), NOW)['decision'], 'BLOCKED')

    def test_exact_net_return_includes_both_fees_without_extra_spread(self):
        self.assertAlmostEqual(net_return_bps(100,101,5,5), 89.90504747626)
        self.assertLess(net_return_bps(100,100.05,5,5), 0)
        with self.assertRaises(ValueError):
            net_return_bps(100,101,-1,5)

    def test_exit_precedence_deadline_stale_and_net_trailing(self):
        p = dict(entry_ts_ms=NOW-10000, entry_price=100, buy_fee_bps=5,
                 sell_fee_bps=5, peak_net_bps=30)
        q = dict(quality_ok=True, quote_ts_ms=NOW, executable_exit_price=99)
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'STOP_LOSS')
        q['executable_exit_price'] = 100.2
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'TRAILING_EXIT')
        p['entry_ts_ms'] = NOW-180000
        q['quote_ts_ms'] = NOW-1501
        self.assertEqual(assess_exit('FAST',p,q,NOW)['action'], 'EXIT_PENDING_DATA')
        q['quote_ts_ms'] = NOW
        p['peak_net_bps'] = 10
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'TIME_LIMIT')
        self.assertEqual(assess_exit('FAST',{},q,NOW)['action'], 'PAUSE_DATA')

    def test_failure_no_followthrough_and_takeprofit(self):
        p = dict(entry_ts_ms=NOW-50000, entry_price=100, buy_fee_bps=5,
                 sell_fee_bps=5, peak_net_bps=0)
        q = dict(quality_ok=True, quote_ts_ms=NOW, executable_exit_price=100.1)
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'NO_FOLLOW_THROUGH')
        q['signal_failed'] = True
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'SIGNAL_FAILURE')
        q['signal_failed'] = False
        q['executable_exit_price'] = 101
        self.assertEqual(assess_exit('FAST',p,q,NOW)['reason'], 'TAKE_PROFIT')

    def test_even_good_evidence_cannot_arm_live(self):
        evidence = dict(forward_days=14, completed_trades=200, independent_episodes=100,
                        oos_trades=100, oos_days=7, oos_profit_factor=1.3,
                        oos_mean_net_bps=5, cluster_ci95_lower_bps=1, stress_mean_net_bps=1,
                        sleeve_drawdown_pct=5, costs_verified=True, chronological_holdout=True,
                        independent_sampling=True, stress_replay_complete=True, recovery_tests_passed=True)
        r = promotion_review(evidence)
        self.assertEqual(r['status'], 'READY_FOR_REVIEW')
        self.assertFalse(r['live_eligible'])
        self.assertTrue(r['manual_release_required'])
        evidence['cluster_ci95_lower_bps'] = 0
        self.assertEqual(promotion_review(evidence)['status'], 'INSUFFICIENT')
        self.assertEqual(promotion_review({})['status'], 'INSUFFICIENT')

    def test_guides_fit_telegram_and_distinguish_paper_from_research(self):
        for strategy in ('fast','wave'):
            text = strategy_text(strategy)
            self.assertLess(len(text.encode('utf-16-le'))//2, 4096)
            for required in ('MACD','RSI','Williams','일봉','미확정','D+1','자동 포착·예약매수·실주문에 연결하지'):
                self.assertIn(required, text)
            self.assertNotIn('시작과 확산', text)
        self.assertFalse(policy()['live_enabled'])


if __name__ == '__main__':
    unittest.main()
