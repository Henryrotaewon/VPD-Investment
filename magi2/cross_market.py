"""Offline cross-market measurement helpers and guide, never trade intents.

No source collection or broker calls. A price gap is not expected PnL. Metadata,
depth VWAP and FX conversion must be supplied by future verified adapters.
"""
import json
from pathlib import Path
from .trade_plan import number


def policy():
    return json.loads(Path(__file__).with_name('cross_market_policy.json').read_text())


def instrument_key(leg):
    """A venue/asset label alone would silently mix spot, perp and expiries."""
    fields = ('venue','asset_id','kind','symbol','quote','settlement')
    if any(not isinstance(leg.get(k),str) or not leg[k] for k in fields):
        raise ValueError('INSTRUMENT_ID_MISSING')
    if leg['kind'] not in policy()['market_kinds']:
        raise ValueError('UNSUPPORTED_MARKET_KIND')
    expiry = leg.get('expiry_ms')
    if leg['kind'] == 'DATED':
        value = number(expiry)
        if value <= 0 or value != value.to_integral_value():
            raise ValueError('EXPIRY_REQUIRED')
        expiry = str(int(value))
    elif expiry is not None:
        raise ValueError('UNEXPECTED_EXPIRY')
    return tuple(leg[k] for k in fields) + (expiry,)


def entry_gap(buy, sell, reporting_currency, now_ms):
    """Quantity-matched VWAP gap, using costly FX ask for buying and bid for selling.

    For derivatives this is a normalized price observation, not a spot cash flow
    or a funded trade. Report currency conversion does not hedge currency risk.
    No fee-adjusted 'profit' is computed without exits/collateral/funding/rebalance.
    """
    cfg = policy()
    result = dict(mode='RESEARCH_ONLY', execution_eligible=False, status='UNAVAILABLE',
                  gross_entry_gap_bps=None, expected_net_profit=None, reasons=[])
    try:
        a, b = instrument_key(buy), instrument_key(sell)
        if a == b or buy['asset_id'] != sell['asset_id']:
            raise ValueError('SAME_INSTRUMENT_OR_DIFFERENT_ASSET')
        if not isinstance(reporting_currency,str) or not reporting_currency:
            raise ValueError('REPORTING_CURRENCY_REQUIRED')
        qt = []
        prices = []
        quantities = []
        for leg, side in ((buy,'ask'),(sell,'bid')):
            if leg.get('metadata_verified') is not True or leg.get('depth_complete') is not True:
                raise ValueError('METADATA_OR_DEPTH_UNVERIFIED')
            if leg['kind'] != 'SPOT' and leg.get('linear') is not True:
                raise ValueError('INVERSE_CONTRACT_UNSUPPORTED')
            if leg['kind'] == 'DATED' and number(leg['expiry_ms']) <= number(now_ms):
                raise ValueError('EXPIRED_CONTRACT')
            t = number(leg['quote_ts_ms']); qt.append(t)
            if not 0 <= number(now_ms)-t <= cfg['max_quote_age_ms']:
                raise ValueError('STALE_OR_FUTURE_QUOTE')
            q = number(leg['base_qty']); quantities.append(q)
            bid, ask = number(leg['bid_vwap']), number(leg['ask_vwap'])
            if not 0 < bid <= ask or q <= 0:
                raise ValueError('INVALID_VWAP_OR_QUANTITY')
            fx = number(1)
            if leg['quote'] != reporting_currency:
                conversion = leg['fx']
                if conversion['from'] != leg['quote'] or conversion['to'] != reporting_currency or conversion['verified'] is not True:
                    raise ValueError('FX_PAIR_UNVERIFIED')
                ft = number(conversion['ts_ms'])
                if not 0 <= number(now_ms)-ft <= cfg['max_fx_age_ms'] or abs(ft-t) > cfg['max_pair_skew_ms']:
                    raise ValueError('FX_TIME_MISMATCH')
                fb, fa = number(conversion['bid']), number(conversion['ask'])
                if not 0 < fb <= fa:
                    raise ValueError('INVALID_FX')
                fx = fa if side == 'ask' else fb
            prices.append((ask if side == 'ask' else bid)*fx)
        if abs(qt[0]-qt[1]) > cfg['max_pair_skew_ms']:
            raise ValueError('PAIR_TIME_MISMATCH')
        if quantities[0] != quantities[1]:
            raise ValueError('BASE_QUANTITY_MISMATCH')
        result.update(status='OBSERVED_GAP', buy_instrument=a, sell_instrument=b,
                      reporting_currency=reporting_currency,
                      gross_entry_gap_bps=float((prices[1]/prices[0]-1)*10000),
                      currency_hedge_required=buy['quote'] != sell['quote'] or buy['settlement'] != sell['settlement'])
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        # Fixed local reasons only, never serialize the entire supplied payload.
        result['reasons'] = [str(exc) if isinstance(exc,ValueError) else 'MISSING_OR_INVALID_INPUT']
    return result


def funding_cashflow(legs, start_ms, end_ms, kind='SCENARIO'):
    """Signed scheduled cash flows over (start,end], NOT rate subtraction.

    Two same-asset perpetual legs, same settlement currency, one long/one short.
    A complete explicit schedule is required; event notional may differ by mark.
    ACTUAL events require settled=True and their signed settled cash amount.
    Future scenarios are never
    merged into actual receipts. There is no default 8h interval or USD=USDT FX.
    """
    try:
        if kind not in ('ACTUAL','SCENARIO') or len(legs) != 2 or number(end_ms) <= number(start_ms):
            raise ValueError('INVALID_WINDOW_OR_LEGS')
        if {x['side'] for x in legs} != {'LONG','SHORT'}:
            raise ValueError('OPPOSITE_LEGS_REQUIRED')
        keys = [instrument_key(x) for x in legs]
        if keys[0] == keys[1] or any(x['kind'] != 'PERPETUAL' for x in legs):
            raise ValueError('DISTINCT_PERPETUAL_LEGS_REQUIRED')
        if legs[0]['asset_id'] != legs[1]['asset_id'] or legs[0]['settlement'] != legs[1]['settlement']:
            raise ValueError('ASSET_OR_SETTLEMENT_MISMATCH')
        total = number(0); count = 0
        for leg in legs:
            if leg.get('metadata_verified') is not True or leg.get('linear') is not True:
                raise ValueError('UNVERIFIED_FUNDING_CONTRACT')
            if leg['schedule_complete'] is not True or number(leg['coverage_start_ms']) > number(start_ms) or number(leg['coverage_end_ms']) < number(end_ms):
                raise ValueError('INCOMPLETE_FUNDING_COVERAGE')
            seen = set()
            for e in leg['events']:
                t = number(e['ts_ms'])
                if not number(start_ms) < t <= number(end_ms):
                    continue
                if t in seen or e['kind'] != kind:
                    raise ValueError('DUPLICATE_OR_MIXED_EVENT')
                seen.add(t)
                if kind == 'ACTUAL' and e.get('settled') is not True:
                    raise ValueError('UNSETTLED_PAYMENT')
                if kind == 'ACTUAL':
                    total += number(e['cashflow_quote'])
                    count += 1
                    continue
                n, rate = number(e['notional_quote']), number(e['rate'])
                if n <= 0:
                    raise ValueError('INVALID_NOTIONAL')
                total += n*rate*(1 if leg['side']=='SHORT' else -1)
                count += 1
        return {'status':'CALCULATED','kind':kind,'settlement':legs[0]['settlement'],
                'net_funding_quote':str(total),'events':count,'execution_eligible':False}
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return {'status':'UNAVAILABLE','kind':kind,'net_funding_quote':None,'execution_eligible':False}


def guide_text():
    p = policy()
    lines = ['🌐 WAVE · 거래소·현선물 통합 연구',
             '확장 준비안 · 현재 수익성이나 실시간 전파망을 검증했다는 뜻은 아닙니다.', '',
             '시장 지도', '거래소 × 현물/무기한/만기 × 종목 × 결제통화를 구분합니다. '
             '현재 6거래소 현물과 Bybit 약 1분 선물 보조지표가 있으며, 초단기 선물 호가·체결망은 추가 구현 대상입니다.', '',
             '해외 전략에서 가져올 연구 항목']
    for row in p['families']:
        lines.append(f'{row["priority"]}순위 · {row["label"]}\n{row["role"]}')
    lines += ['', 'WAVE가 판단할 것',
              '어디서 시작됐는지 → 현물·선물 어디로 확산하는지 → 계속 확산/가격차 수렴/과열 중 어느 가설이 맞는지 → 확인 후 비용을 빼고도 거래할 여지가 있는지.',
              '선물 급등·OI 증가만으로 신규 매수세를 단정하지 않습니다. 청산 연쇄·시장 전체 동조·시차 오류도 비교합니다.', '',
              '실행 가능한 차익과 가격차 구별',
              'A 현금·B 코인 또는 증거금을 사전에 배치. 신호 후 송금 완료를 기다리는 구조는 초단기 차익에서 제외합니다. '
              '양쪽 깊이·수수료·환율·펀딩 지급시각·재고 회복 비용·각 거래소의 청산 위험을 따로 반영합니다. '
              '해외 이익으로 국내외 다른 계좌의 증거금 부족을 자동 상쇄하지 않습니다.', '',
              '적용 순서',
              '① WAVE 선행·후행과 FAST 단독/전파 확인 비교\n② 거래소 간 BASIS·펀딩·재고 차익 모의검증\n③ 만기 스프레드·시장조성 검토',
              'BASIS의 동일 거래소 v1은 비교 기준선으로 유지합니다. 분석은 크로스 거래소까지 확장하며, 실행 모델은 별도로 검증합니다.', '',
              '현재: 문헌 검토·상품 식별·환율 반영 가격차·지급일정별 펀딩 계산 준비. '
              '새 수집기·자동 모의매매·실주문 미연결 · 실제 배정자금 0.']
    return '\n'.join(lines)
