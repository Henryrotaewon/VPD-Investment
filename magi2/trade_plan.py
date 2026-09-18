"""Pure research decisions. No collector, timer, ledger, signal publisher or broker.

PAPER_CANDIDATE is only an offline decision: runners do not call these functions.
Inputs must be measured at decision time, with no retrospective WAVE catalog
substituted for the separately specified live confirmation contract.
"""
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
from pathlib import Path


def policy():
    return json.loads(Path(__file__).with_name('trade_policy.json').read_text())


def number(value):
    if isinstance(value, bool):
        raise ValueError('INVALID_NUMBER')
    try:
        value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('INVALID_NUMBER')
    if not value.is_finite():
        raise ValueError('INVALID_NUMBER')
    return value


def net_return_bps(entry_price, exit_price, buy_fee_bps, sell_fee_bps):
    """Executable VWAPs include spread/slippage already; never deduct them twice."""
    entry, exit_ = number(entry_price), number(exit_price)
    buy, sell = number(buy_fee_bps), number(sell_fee_bps)
    if min(entry, exit_) <= 0 or not (0 <= buy < 10000 and 0 <= sell < 10000):
        raise ValueError('INVALID_PRICE_OR_FEE')
    return float((exit_ * (1 - sell / 10000) / (entry * (1 + buy / 10000)) - 1) * 10000)


def assess_entry(strategy, setup, capital, now_ms):
    """Return a research decision and proposed notional, never an order intent.

    Capital exposure includes FAST+WAVE holdings at max(cost, mark), unresolved
    orders and entry fees, including other venues. `free_cash_krw` is available
    cash after every strategy's outstanding reservations, without double-counting
    exchange locks. The supplied snapshot must be complete for the declared scope.
    """
    cfg = policy()
    if strategy not in ('FAST', 'WAVE'):
        raise ValueError('UNKNOWN_STRATEGY')
    c, p = cfg['common'], cfg[strategy]
    reasons = []
    notional = Decimal(0)

    def require(condition, reason):
        if not condition:
            reasons.append(reason)

    def age(stamp, limit):
        return 0 <= number(now_ms) - number(stamp) <= limit

    try:
        require(setup['venue'] in c['initial_execution_venues'] and isinstance(setup['market'], str) and setup['market'].startswith('KRW-'), 'EXECUTION_VENUE')
        require(setup['side'] == 'BUY', 'SPOT_LONG_ONLY')
        require(setup['market_active'] is True, 'MARKET_NOT_ACTIVE')
        require(setup['quality_ok'] is True and age(setup['quote_ts_ms'], c['max_quote_age_ms']), 'QUOTE_QUALITY')
        require(age(setup['decision_ts_ms'], p.get('signal_ttl_ms', 1500)), 'SIGNAL_EXPIRED')
        require(capital['complete'] is True and age(capital['observed_ts_ms'], c['max_capital_age_ms']), 'CAPITAL_QUALITY')
        require(capital['asset_busy'] is False, 'ASSET_ALREADY_OWNED_OR_PENDING')
        counters = [number(capital[k]) for k in ('unresolved_orders', 'positions', 'strategy_positions', 'entries_today', 'episode_entries', 'consecutive_losses')]
        require(all(x >= 0 and x == x.to_integral_value() for x in counters), 'INVALID_COUNTER')
        require(number(capital['unresolved_orders']) == 0, 'ORDER_RECONCILIATION_REQUIRED')
        require(number(capital['positions']) < c['max_positions'] and number(capital['strategy_positions']) < p['max_positions'], 'POSITION_LIMIT')
        require(number(capital['entries_today']) < c['max_entries_per_kst_day'], 'DAILY_ENTRY_LIMIT')
        require(number(capital['episode_entries']) < p['max_entries_per_episode'], 'EPISODE_ENTRY_LIMIT')
        require(number(now_ms) >= number(capital['pause_until_ms']), 'LOSS_PAUSE')
        require(number(capital['consecutive_losses']) < c['max_consecutive_losses'], 'LOSS_STREAK')
        if capital['last_exit_ts_ms'] is not None:
            require(number(now_ms) - number(capital['last_exit_ts_ms']) >= p['cooldown_sec'] * 1000, 'REENTRY_COOLDOWN')
            require(setup['fresh_setup_after_exit'] is True, 'NEW_SETUP_REQUIRED')
        require(setup['signal_consumed'] is False, 'SIGNAL_ALREADY_CONSUMED')

        bid, ask = number(setup['bid']), number(setup['ask'])
        require(0 < bid <= ask, 'INVALID_BOOK')
        if bid <= 0 or ask <= 0:
            raise ValueError('INVALID_BOOK')
        spread = (ask / bid - 1) * 10000
        require(spread <= p['max_spread_bps'], 'SPREAD_LIMIT')
        require(0 <= number(setup['chase_bps']) <= p['max_chase_bps'], 'CHASE_LIMIT')
        require(p['min_buyer_share_pct'] <= number(setup['buyer_share_pct']) <= 100, 'BUY_FLOW')
        require(number(setup['sample_trades']) >= p['min_trades'] and number(setup['sample_span_ms']) >= p['min_trade_span_ms'] and 0 <= number(setup['latest_trade_age_ms']) <= p['max_trade_age_ms'], 'TRADE_SAMPLE')
        fees = [number(setup[k]) for k in ('buy_fee_bps', 'sell_fee_bps')]
        friction = [number(setup[k]) for k in ('entry_slippage_bps', 'exit_slippage_bps', 'latency_buffer_bps')]
        require(setup['fees_verified'] is True and age(setup['fee_ts_ms'], c['max_fee_age_ms']) and all(0 <= x < 10000 for x in fees), 'FEE_UNKNOWN')
        require(all(x >= 0 for x in friction), 'INVALID_COST')
        require(spread + sum(fees + friction) <= p['max_roundtrip_cost_bps'], 'COST_LIMIT')

        if strategy == 'FAST':
            require(number(setup['rise_5m_bps']) >= p['min_rise_5m_bps'], 'RISE_TOO_WEAK')
            require(1 <= number(setup['rank']) <= p['top_n'] or number(setup['rank_jump']) >= p['min_rank_jump'], 'RANK_FILTER')
            require(number(setup['lookback_ms']) >= p['breakout_lookback_sec'] * 1000 and number(setup['breakout_bps']) >= p['min_breakout_bps'], 'BREAKOUT_REQUIRED')
            require(0 <= number(now_ms) - number(setup['watch_started_ms']) < p['watch_sec'] * 1000, 'WATCH_EXPIRED')
        else:
            require(setup['source_contract'] == p['source_contract'], 'WAVE_LIVE_CONTRACT_REQUIRED')
            require(setup['independent_triggers'] is True and setup['timing_resolved'] is True and setup['market_residual_positive'] is True, 'WAVE_EVIDENCE_REQUIRED')
            venues = setup['confirming_venues']
            require(isinstance(venues, list) and all(isinstance(x, str) for x in venues) and len(set(venues) - {setup['venue']}) >= p['min_other_venues'], 'INDEPENDENT_CONFIRMATION_REQUIRED')
            require(number(setup['origin_ts_ms']) <= number(setup['confirmed_ts_ms']) <= number(setup['decision_ts_ms']) <= number(now_ms), 'WAVE_EVENT_ORDER')
            require(0 <= number(setup['confirmed_ts_ms']) - number(setup['origin_ts_ms']) <= p['confirmation_window_ms'] and age(setup['confirmed_ts_ms'], p['signal_ttl_ms']), 'WAVE_CONFIRMATION_EXPIRED')
            require(number(setup['origin_return_z']) >= p['min_origin_return_z'] and number(setup['origin_volume_z']) >= p['min_origin_volume_z'], 'WAVE_NOISE_FLOOR')
            require(number(setup['destination_return_15s_bps']) >= p['min_destination_return_15s_bps'], 'DESTINATION_NOT_RESPONDING')

        equity, cash, exposure = [number(capital[k]) for k in ('equity_krw', 'free_cash_krw', 'shared_exposure_krw')]
        require(equity > 0 and min(cash, exposure) >= 0, 'INVALID_CAPITAL')
        start_equity = number(capital['day_start_equity_krw'])
        require(start_equity > 0, 'INVALID_DAY_START_EQUITY')
        require(number(capital['daily_pnl_krw']) > -start_equity * number(c['daily_loss_equity_bps']) / 10000, 'DAILY_LOSS_LIMIT')
        # Both sides must have enough visible depth inside the allowed price band.
        depth = min(number(setup['ask_depth_krw']), number(setup['bid_depth_krw']))
        require(depth > 0, 'DEPTH_UNAVAILABLE')
        inclusive_budget = min(equity * number(c['entry_capital_pct']) / 100,
                               equity * number(c['shared_capital_pct']) / 100 - exposure, cash)
        notional = max(Decimal(0), min(inclusive_budget / (1 + fees[0] / 10000),
                                     depth * number(c['depth_participation_pct']) / 100))
        notional = notional.quantize(Decimal('1'), rounding=ROUND_DOWN)
        minimum = number(setup['min_order_krw'])
        require(minimum > 0 and notional >= minimum, 'MINIMUM_OR_BUDGET')
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError):
        reasons.append('MISSING_OR_INVALID_INPUT')
    return {'policy_version': cfg['version'], 'strategy': strategy,
            'decision': 'BLOCKED' if reasons else 'PAPER_CANDIDATE',
            'reasons': list(dict.fromkeys(reasons)),
            'notional_krw': str(notional) if not reasons else '0',
            'live_eligible': False, 'order_submitted': False}


def assess_exit(strategy, position, observation, now_ms):
    """Request an offline exit; no mark is a fill, and stale quotes cannot fill it.

    Position contains actual/simulated entry VWAP and inclusive fee rates.
    executable_exit_price is depth VWAP after exit slippage; peak is the best
    previously observed net liquidation return, never a future high.
    """
    cfg = policy()
    if strategy not in ('FAST', 'WAVE'):
        raise ValueError('UNKNOWN_STRATEGY')
    p = cfg[strategy]
    deadline = False
    try:
        elapsed = number(now_ms) - number(position['entry_ts_ms'])
        if elapsed < 0:
            raise ValueError('FUTURE_ENTRY')
        deadline = elapsed >= p['max_holding_sec'] * 1000
        fresh = observation['quality_ok'] is True and 0 <= number(now_ms) - number(observation['quote_ts_ms']) <= cfg['common']['max_quote_age_ms']
        if not fresh:
            return {'action': 'EXIT_PENDING_DATA' if deadline else 'PAUSE_DATA', 'reason': 'STALE_QUOTE', 'net_bps': None}
        net = net_return_bps(position['entry_price'], observation['executable_exit_price'], position['buy_fee_bps'], position['sell_fee_bps'])
        peak = max(number(position['peak_net_bps']), number(net))
        reason = None
        if net <= p['stop_loss_net_bps']:
            reason = 'STOP_LOSS'
        elif observation.get('signal_failed') is True:
            reason = 'SIGNAL_FAILURE'
        elif net >= p['take_profit_net_bps']:
            reason = 'TAKE_PROFIT'
        elif peak >= p['trail_arm_net_bps'] and peak - number(net) >= p['trail_drawdown_bps']:
            reason = 'TRAILING_EXIT'
        elif deadline:
            reason = 'TIME_LIMIT'
        elif elapsed >= p['stall_sec'] * 1000 and peak < p['stall_min_peak_net_bps']:
            reason = 'NO_FOLLOW_THROUGH'
        return {'action': 'EXIT_CANDIDATE' if reason else 'HOLD', 'reason': reason,
                'net_bps': net, 'peak_net_bps': float(peak)}
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError):
        return {'action': 'EXIT_PENDING_DATA' if deadline else 'PAUSE_DATA', 'reason': 'MISSING_OR_INVALID_INPUT', 'net_bps': None}


def promotion_review(metrics):
    """Checklist from independently produced evidence, never an automatic release.

    Run separately per strategy/venue/frozen policy version. Cluster confidence
    intervals and stress replay must come from evaluation code, not this helper.
    """
    p = policy()['promotion']
    missing = []
    minimums = {'forward_days': 'min_forward_days', 'completed_trades': 'min_completed_trades',
                'independent_episodes': 'min_independent_episodes', 'oos_trades': 'min_oos_trades',
                'oos_days': 'min_oos_days', 'oos_profit_factor': 'min_oos_profit_factor'}
    try:
        for field, limit in minimums.items():
            if number(metrics[field]) < number(p[limit]):
                missing.append(field)
        for field in ('oos_mean_net_bps', 'cluster_ci95_lower_bps', 'stress_mean_net_bps'):
            if number(metrics[field]) <= 0:
                missing.append(field)
        if not 0 <= number(metrics['sleeve_drawdown_pct']) <= p['max_sleeve_drawdown_pct']:
            missing.append('sleeve_drawdown_pct')
        for field in ('costs_verified', 'chronological_holdout', 'independent_sampling',
                      'stress_replay_complete', 'recovery_tests_passed'):
            if metrics[field] is not True:
                missing.append(field)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        missing.append('MISSING_OR_INVALID_EVIDENCE')
    return {'status': 'INSUFFICIENT' if missing else 'READY_FOR_REVIEW',
            'missing': missing, 'live_eligible': False, 'manual_release_required': True}


def strategy_plan_text(strategy):
    cfg = policy()
    p, c = cfg[strategy], cfg['common']
    pct = lambda bps: f'{bps / 100:+.2f}%'
    if strategy == 'FAST':
        title = '⚡ FAST · 초기 돌파 반복 매매'
        entry = (f'① {p["scan_sec"]//60}분 탐색 → 거래소 상위 {p["top_n"]}개 또는 순위 +{p["min_rank_jump"]} 이상, 5분 +{p["min_rise_5m_bps"]/100:g}% 이상 → {p["watch_sec"]//60}분 집중 관찰.\n'
                 f'② 직전 {p["breakout_lookback_sec"]}초 고점 +{p["min_breakout_bps"]/100:.2f}% 돌파, 매수 주도 거래대금 {p["min_buyer_share_pct"]}% 이상·20체결/5초 이상 표본.\n'
                 f'③ 후보 선정 후 추가 상승 {p["max_chase_bps"]/100:.2f}% 이내. 늦은 추격은 건너뜁니다.')
        progress = '현재: 강한 매수세 포착·알림·5분 가격 관측 운영 중. 이 매매안의 자동 모의진입·체결·청산 연결은 준비 단계입니다.'
        failure = '돌파 가격 아래로 3초 연속 후퇴하면 청산 후보.'
    else:
        title = '🌐 WAVE · 확산 확인 후 후행 시장 진입'
        entry = (f'① 평소 대비 가격 이례성 z≥{p["min_origin_return_z"]}, 거래량 z≥{p["min_origin_volume_z"]}의 매수 충격.\n'
                 f'② 진입 거래소 외 {p["min_other_venues"]}개 거래소에서 {p["confirmation_window_ms"]//1000}초 내 같은 방향을 독립 확인. 수신 지연·시장 전체 동반 상승을 구분합니다.\n'
                 f'③ 진입 시장 15초 +{p["min_destination_return_15s_bps"]/100:.2f}% 이상·매수 비중 {p["min_buyer_share_pct"]}% 이상, 충격 전 대비 추격 {p["max_chase_bps"]/100:.2f}% 이내. 확인 후 {p["signal_ttl_ms"]//1000}초 내 판단.')
        progress = '현재: 시간 이동 대조군을 이용한 탐색 분석. 독립 실시간 확인·이례성 보정·확인 후 매매 성적은 아직 미구현입니다. 5분 갱신 분석 화면을 주문 신호로 쓰지 않습니다. WHALE은 추가 예측력 검증용 보조자료입니다.'
        failure = '선행 시장 반전과 진입 시장 매수세 약화가 3초 지속되면 청산 후보.'
    return (title + '\n\n매매 준비안 v1 · 수치는 검증 전 가설\n' + entry + '\n\n'
            f'주문 조건\n스프레드 ≤{p["max_spread_bps"]/100:.2f}%, 왕복 비용 예산 ≤{p["max_roundtrip_cost_bps"]/100:.2f}%. '
            '계좌별 수수료·호가 깊이·지연을 확인하고, 유효 호가 잔량의 10% 이내로 제한합니다.\n\n'
            f'청산\n순익 {pct(p["take_profit_net_bps"])} 목표 / 순손실 {pct(p["stop_loss_net_bps"])} 손절 신호 / 최대 {p["max_holding_sec"]//60}분. '
            f'순익 {pct(p["trail_arm_net_bps"])} 이후 고점 대비 {p["trail_drawdown_bps"]/100:.2f}%p 반납 시 청산 후보. '
            f'{p["stall_sec"]}초 동안 순익 고점이 {pct(p["stall_min_peak_net_bps"])}에 못 미치면 종료. ' + failure + ' 체결 지연으로 실제 손실은 기준을 넘을 수 있습니다.\n\n'
            f'재진입·자금\n청산 후 {p["cooldown_sec"]}초 대기·새 신호 필요, 같은 사건 최대 {p["max_entries_per_episode"]}회. 물타기·추가매수 없음. '
            f'1회 총자산 {c["entry_capital_pct"]:g}% 이내, FAST+WAVE 합계 {c["shared_capital_pct"]:g}% 이내·동시 {c["max_positions"]}종목. '
            f'합산 하루 최대 {c["max_entries_per_kst_day"]}회이며 횟수는 목표가 아닙니다. 일손실 총자산 -{c["daily_loss_equity_bps"]/100:.2f}%면 신규 진입 중단, 3연속 손실이면 30분 휴식.\n\n'
            '검증 통과안\n전략·거래소별 14일·200회 이상, 독립 사건 100개, 미사용 검증기간 7일·100회 이상. '
            '비용 차감 평균손익의 군집 신뢰구간 하단 >0, 손익비율(PF) ≥1.2, 비용·지연 스트레스 후 순익 확인. 자동 실거래 전환 없음.\n\n' + progress + '\n실주문 OFF · 초기 실행 검증은 업비트 KRW부터, 관측망은 유지합니다.')
