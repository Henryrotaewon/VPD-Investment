"""Offline spot-long / linear-futures-short planning. No exchange/order adapter."""
import json
from pathlib import Path
from .trade_plan import number


def policy():
    return json.loads(Path(__file__).with_name('basis_policy.json').read_text())


def stressed_funding_receipt(notional, predicted_rate, past_positive_median):
    """One scheduled payment scenario; rates are fractions, not bps.

    The caller must supply the actual contract schedule and a past-only baseline.
    This estimate is neither a guaranteed payment nor a calibrated lower bound.
    """
    n, rate, median = map(number, (notional, predicted_rate, past_positive_median))
    if n <= 0 or median < 0:
        raise ValueError('INVALID_FUNDING_INPUT')
    effective = rate if rate < 0 else min(rate, median) * number(policy()['PERPETUAL']['positive_funding_haircut'])
    return str(n * effective)


def paired_pnl(*, base_qty, spot_entry, future_entry, spot_exit, future_exit,
               spot_entry_fee_bps, future_entry_fee_bps, spot_exit_fee_bps,
               future_exit_fee_bps, funding_received, other_costs,
               initial_margin, cash_reserve):
    """Same base quantity and quote currency, linear short only.

    Prices are executable VWAPs (no spread/slippage double subtraction).
    Positive funding is cash RECEIVED by the short; negative means PAID.
    Inputs may describe a scenario or actual fills: output is arithmetic, not a
    forecast. Return denominator includes spot cost, entry fees, margin, reserve.
    """
    q, s0, f0, s1, f1 = map(number, (base_qty, spot_entry, future_entry, spot_exit, future_exit))
    rates = list(map(number, (spot_entry_fee_bps, future_entry_fee_bps, spot_exit_fee_bps, future_exit_fee_bps)))
    funding, other, margin, reserve = map(number, (funding_received, other_costs, initial_margin, cash_reserve))
    if min(q,s0,f0,s1,f1) <= 0 or min(other,margin,reserve) < 0 or not all(0 <= r < 10000 for r in rates):
        raise ValueError('INVALID_PAIR_INPUT')
    entry_fees = q * (s0*rates[0] + f0*rates[1]) / 10000
    fees = entry_fees + q * (s1*rates[2] + f1*rates[3]) / 10000
    capital = q*s0 + entry_fees + margin + reserve
    spot_pnl, future_pnl = q*(s1-s0), q*(f0-f1)
    net = spot_pnl + future_pnl + funding - fees - other
    return {k: str(v) for k,v in dict(spot_pnl=spot_pnl, future_pnl=future_pnl,
            funding_received=funding, fees=fees, other_costs=other, net_pnl=net,
            committed_capital=capital, return_on_capital_bps=net/capital*10000).items()}


def hedge_action(spot_base_filled, short_base_filled, unhedged_ms, outcome_known=True):
    """State advice only; never infer that an unknown leg failed or send a hedge."""
    p = policy()
    if outcome_known is not True:
        return 'RECONCILE_BOTH_LEGS'
    try:
        spot, short, age = map(number, (spot_base_filled, short_base_filled, unhedged_ms))
        if min(spot,short,age) < 0:
            raise ValueError('INVALID_FILL')
        if spot == short == 0:
            return 'NO_POSITION'
        mismatch = abs(spot-short)/max(spot,short)*100
        if mismatch <= number(p['max_delta_mismatch_pct']):
            return 'HEDGED'
        return 'UNWIND_EXCESS_FILLED_LEG' if age >= p['max_unhedged_ms'] else 'HEDGE_PENDING'
    except (ValueError, TypeError):
        return 'RECONCILE_BOTH_LEGS'


def assess_basis_setup(snapshot, now_ms):
    """Requires externally computed stressed economics; missing data blocks.

    Carry/funding/basis/fees are NOT inferred from the last funding rate.
    Even a passing plan leaves allocated real capital and live eligibility zero.
    """
    p = policy()
    reasons = []
    def require(condition, reason):
        if not condition:
            reasons.append(reason)
    try:
        s = snapshot
        require(s['asset'] in p['universe'], 'ASSET_SCOPE')
        require(isinstance(s['spot_venue'], str) and bool(s['spot_venue']) and s['spot_venue'] == s['future_venue'], 'SAME_VENUE_REQUIRED')
        require(s['spot_quote'] in ('USDT','USDC','USD') and s['spot_quote'] == s['future_quote'] == s['settlement_currency'], 'SAME_QUOTE_REQUIRED')
        require(s['linear_contract'] is True and s['contract_verified'] is True, 'CONTRACT_NOT_VERIFIED')
        require(s['borrowed'] is False, 'BORROWING_EXCLUDED')
        require(s['fees_verified'] is True and s['costs_complete'] is True and s['margin_data_verified'] is True, 'COST_OR_MARGIN_UNKNOWN')
        st, ft = number(s['spot_quote_ts_ms']), number(s['future_quote_ts_ms'])
        require(all(0 <= number(now_ms)-t <= p['max_quote_age_ms'] for t in (st,ft)) and abs(st-ft) <= p['max_pair_skew_ms'], 'QUOTE_QUALITY')
        qty, spot, future = map(number, (s['base_qty'], s['spot_ask'], s['future_bid']))
        require(min(qty,spot,future) > 0, 'INVALID_SIZE_OR_PRICE')
        require(s['rounded_hedge_verified'] is True, 'CONTRACT_SIZE_OR_STEP')
        require(hedge_action(qty,s['short_base_qty'],0) == 'HEDGED', 'DELTA_MISMATCH')
        notional = qty*future
        require(number(s['initial_margin']) >= notional*number(p['min_margin_to_futures_notional']), 'MARGIN_BUFFER')
        require(number(s['cash_reserve']) >= notional*number(p['min_cash_reserve_to_futures_notional']), 'CASH_BUFFER')
        require(s['free_capital_covers_both_legs'] is True, 'CAPITAL_SHORTFALL')
        require(s['scenario_asof_ms'] <= now_ms and 0 <= number(now_ms)-number(s['scenario_asof_ms']) <= p['max_quote_age_ms'], 'SCENARIO_EXPIRED')
        net = number(s['stressed_net_return_on_capital_bps'])
        if s['contract_type'] == 'DATED':
            d = p['DATED']; days = number(s['days_to_expiry'])
            require(d['min_days_to_expiry'] <= days <= d['max_days_to_expiry'], 'EXPIRY_WINDOW')
            require(future > spot, 'NO_POSITIVE_BASIS')
            require(s['settlement_alignment_verified'] is True, 'SETTLEMENT_MISMATCH')
            require(net >= d['min_net_return_on_capital_bps'], 'NET_CARRY_TOO_SMALL')
            require(days > 0 and net/100*365/days >= d['min_net_simple_annualized_pct'], 'CAPITAL_EFFICIENCY')
        elif s['contract_type'] == 'PERPETUAL':
            f = p['PERPETUAL']
            require(number(s['next_funding_rate']) >= 0, 'NEGATIVE_FUNDING')
            require(number(s['funding_history_days']) >= f['funding_lookback_days'], 'FUNDING_HISTORY')
            require(number(f['min_positive_funding_fraction']) <= number(s['positive_funding_fraction']) <= 1, 'FUNDING_PERSISTENCE')
            require(s['funding_schedule_verified'] is True and s['conservative_funding_scenario'] is True, 'FUNDING_SCENARIO_REQUIRED')
            require(number(s['scenario_horizon_hours']) == f['review_horizon_hours'], 'SCENARIO_HORIZON')
            require(net >= f['min_stressed_net_return_on_capital_bps'], 'FUNDING_NET_EDGE_TOO_SMALL')
        else:
            reasons.append('UNSUPPORTED_CONTRACT')
    except (KeyError, TypeError, ValueError, ArithmeticError):
        reasons.append('MISSING_OR_INVALID_INPUT')
    return {'policy_version':p['version'], 'decision':'BLOCKED' if reasons else 'RESEARCH_CANDIDATE',
            'reasons':list(dict.fromkeys(reasons)), 'live_eligible':False,
            'allocated_real_capital_pct':0, 'order_submitted':False}


def basis_text():
    p = policy(); d, f = p['DATED'], p['PERPETUAL']
    return ('⚖️ BASIS · 현물·선물 차익전략 준비\n\n'
            '기본 비교안\n동일 거래소·동일 결제통화에서 BTC/ETH 현물 매수 + 같은 코인 수량의 선물 매도. '
            '선물 계약 수를 코인 수량으로 환산하고 양쪽 체결을 맞춥니다.\n\n'
            '① 만기형 · 가격차 수렴\n'
            f'만기 {d["min_days_to_expiry"]}~{d["max_days_to_expiry"]}일, 선물 매도호가가 현물 매수호가보다 높고 '
            f'4회 거래 수수료·슬리피지·자금비용을 뺀 총투입자본 수익률 ≥{d["min_net_return_on_capital_bps"]/100:.2f}%, '
            f'단순 연환산 ≥{d["min_net_simple_annualized_pct"]}%를 검증 기준안으로 둡니다. 만기 결제지수와 현물 청산가격 차이도 평가합니다.\n\n'
            '② 무기한형 · 펀딩 수취\n'
            '양의 펀딩에서는 숏이 수취하지만 이후 음수로 바뀔 수 있습니다. '
            f'최근 {f["funding_lookback_days"]}일의 양의 지급 비율 ≥{f["min_positive_funding_fraction"]*100:g}%, '
            f'{f["review_horizon_hours"]}시간 보수 시나리오의 총투입자본 순수익 ≥{f["min_stressed_net_return_on_capital_bps"]/100:.2f}%를 검토합니다. '
            '예상 양의 펀딩은 절반만 인정하고 음의 펀딩은 전액 비용, 청산 시 가격차 0.30%p 확대도 반영합니다. '
            f'최대 {f["max_holding_hours"]}시간 후 재평가하며 관성적으로 연장하지 않습니다.\n\n'
            '자금·체결\n현물 매수금 + 선물 명목금액 100% 이상 증거금 + 20% 여유현금 + 진입 수수료를 따로 확보합니다. '
            '차입 없음. 순수익률 분모에는 이 자금 전체를 넣습니다. 1배 수준 증거금도 청산 위험을 없애지는 않습니다. '
            '양쪽 수량 오차 0.1% 이내. 2초 이상 불일치하면 초과 체결분 축소, 응답 불명확이면 양쪽 주문부터 대사합니다.\n\n'
            '청산 기준안\n쌍 전체 순손실 −0.50%, 청산가격까지 거리 30% 미만, 유지증거금/마진잔고 30% 이상, '
            '평가 증거금/선물 명목금액 75% 미만이면 축소·청산 검토. 펀딩이 음수로 바뀌면 다음 지급 전 재평가합니다.\n\n'
            '현재 단계\n설계·계산·헤지 판단 함수 준비. 수집·자동 모의매매·실거래 미연결, 실제 배정자금 0. '
            'FAST/WAVE와 별도 원장·검증 대상으로 둡니다. 거래소 간 현선물·펀딩 차이와 WAVE 연계는 크로스마켓 연구에서 준비합니다. '
            '업비트 현물+해외선물은 환율·김치프리미엄·거래소별 담보 영향을 추가 평가합니다. '
            '모든 기준은 검증 전 가설이며 무위험 확정수익을 뜻하지 않습니다.')
