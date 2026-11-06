"""One ARK entry, a partial protective exit and final daily trend exit.

Reuses archived candles and the previously specified A/C signal definitions.
This is a descriptive scenario selected after viewing those earlier outcomes.
"""
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from research.september_exit_validation.replay import (
    daily_states, read_gzip, simulate, validate,
)

ROOT = Path(__file__).resolve().parent
STUDY = ROOT.parent / 'september_exit_validation'
KST = ZoneInfo('Asia/Seoul')


def clock(ms):
    return datetime.fromtimestamp(ms / 1000, KST).strftime('%Y-%m-%d %H:%M')


def first_exits(rows, states, entry, end, prior_policy):
    return {m: simulate(rows, states, entry, end, m, prior_policy) for m in ('A', 'C')}


def build_ledger(outcomes, capital, fraction, fee, slip):
    a, c = outcomes['A'], outcomes['C']
    entry_price = a['entry_reference']
    assert a['entry_ms'] == c['entry_ms'] and entry_price == c['entry_reference']
    buy_price = entry_price * (1 + slip)
    quantity = capital / (buy_price * (1 + fee))
    if a['exit_ms'] <= c['exit_ms']:
        legs = [(1., a)]
    else:
        legs = [(fraction, c), (1 - fraction, a)]
    cash = 0.
    transactions = []
    for weight, out in legs:
        qty = quantity * weight
        sell_price = out['exit_reference'] * (1 - slip)
        sale_value = qty * sell_price
        fee_value = sale_value * fee
        proceeds = sale_value - fee_value
        cash += proceeds
        transactions.append(dict(
            time_kst=clock(out['exit_ms']), reference_price_krw=out['exit_reference'],
            assumed_fill_price_krw=sell_price, fraction_of_original_quantity=weight,
            quantity=qty, sale_fee_krw=fee_value, net_proceeds_krw=proceeds,
            cumulative_cash_krw=cash, reason=out['reason'],
            signal_available_kst=clock(out['signal_available_ms']),
            allocated_cost_krw=capital*weight,
            leg_net_pct=(proceeds/(capital*weight)-1)*100,
        ))
    weighted_reference = sum(w * out['exit_reference'] for w, out in legs)
    net = (cash / capital - 1) * 100
    assert math.isclose(sum(t['quantity'] for t in transactions), quantity, rel_tol=1e-12)
    assert math.isclose(net, sum(w*out['net_pct'] for w, out in legs), abs_tol=1e-10)
    assert math.isclose(cash, quantity*weighted_reference*(1-slip)*(1-fee), rel_tol=1e-12)
    return dict(
        initial_capital_krw=capital,
        entry=dict(time_kst=clock(a['entry_ms']), reference_price_krw=entry_price,
                   assumed_fill_price_krw=buy_price, quantity=quantity,
                   buy_fee_krw=quantity*buy_price*fee, total_cost_krw=capital),
        sells=transactions, weighted_exit_reference_krw=weighted_reference,
        gross_pct=(weighted_reference/entry_price-1)*100,
        net_pct=net, final_cash_krw=cash, net_profit_krw=cash-capital,
        final_exit_kst=transactions[-1]['time_kst'], remaining_quantity=0.,
    )


def main():
    policy = json.loads((ROOT/'split_policy.json').read_text())
    prior = json.loads((STUDY/'policy.json').read_text())
    assert policy['fee_per_side'] == prior['fee_per_side']
    assert policy['slippage_per_side'] == prior['slippage_per_side']
    entry = int(datetime.fromisoformat(policy['entry_utc']).timestamp()*1000)
    end = entry + prior['evaluation_days']*86400000
    rows = read_gzip(STUDY/'data/KRW-ARK_5m.json.gz')
    daily = read_gzip(STUDY/'data/daily_all.json.gz')['KRW-ARK']
    validate(rows, daily, entry, end, prior)
    states = daily_states(daily)
    outcomes = first_exits(rows, states, entry, end, prior)
    # Each exit must survive deletion of subsequent prices; the fill bar's open
    # is retained, but its future high/low/close/volume cannot inform the exit.
    for method, out in outcomes.items():
        prefix = [r[:] for r in rows if r[0] <= out['exit_ms']]
        prefix[-1][2:5] = [prefix[-1][1]]*3
        prefix[-1][5:] = [0, 0]
        earlier = simulate(prefix, {t:v for t,v in states.items() if t<=out['exit_ms']},
                           entry, out['exit_ms'], method, prior)
        assert earlier == out, 'FUTURE_DATA_CHANGED_EXIT'
    ledger = build_ledger(outcomes, policy['initial_capital_krw'],
                          policy['partial_fraction_of_original_quantity'],
                          policy['fee_per_side'], policy['slippage_per_side'])
    # If the daily exit arrives first, no later protective trade is permissible.
    reversed_order = {m:dict(o) for m,o in outcomes.items()}
    reversed_order['A']['exit_ms'] = outcomes['C']['exit_ms']
    check = build_ledger(reversed_order, policy['initial_capital_krw'], .5,
                         policy['fee_per_side'], policy['slippage_per_side'])
    assert len(check['sells']) == 1 and check['sells'][0]['fraction_of_original_quantity'] == 1
    result = dict(policy=policy, ledger=ledger, reference_signals=outcomes,
                  quality_checks=['DAILY_5M_OHLCV_MATCH', 'FUTURE_TRUNCATION_INVARIANT',
                                  'QUANTITY_AND_CASH_CONSERVATION', 'DAILY_EXIT_PRIORITY'],
                  comparators={m:dict(exit_kst=clock(o['exit_ms']), price=o['exit_reference'],
                                      net_pct=o['net_pct']) for m,o in outcomes.items()})
    (ROOT/'split_simulation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    report = [
        '# 아크 9월 단일 거래: 50% 분할매도 시뮬레이션', '',
        f"**비용 반영 최종 수익률 {ledger['net_pct']:+.2f}%**. 300만원 가정 시 최종 현금 {ledger['final_cash_krw']:,.0f}원, 순이익 {ledger['net_profit_krw']:,.0f}원이다.", '',
        '연구용 가정이다. 9월 전체의 자동 포착·재진입을 재현한 백테스트가 아니라, 사용자가 지정한 9월 11일 포착 → 12일 진입 사례 한 차례를 계산했다. 앞선 A/C 결과를 본 뒤 50:50으로 조합했으므로 독립 검증 결과가 아니며 운영에는 적용하지 않았다.', '',
        '## 거래 조건', '',
        '- 2026년 9월 12일 09:00 KST에 100% 매수. 11일 일봉을 보고 다음 일봉 시작에 진입한다는 사례 가정이다. 11일 자동 포착이 검증됐다는 뜻은 아니다.',
        '- 보유 고점이 매수 기준가격보다 12% 이상 상승한 뒤, 고점 대비 5% 아래로 확정 5분봉 종가가 내려오면 다음 실제 5분봉 시가에 최초 수량의 50%를 매도한다.',
        '- 잔여 50%는 MACD 히스토그램·RSI14·Williams14 중 두 지표 이상이 전일보다 하락한 일봉이 확정되면 다음 일봉 시작에 매도한다. 이 조건이 먼저 나오면 전량 매도한다.',
        '- 분할매도는 1회만 한다. 재진입·매도대금 재투자·추가 손절은 없다. 5일 공통 관찰 종료 규칙을 상속하지만 이번 거래는 모두 신호로 종료됐다.',
        '- 수수료는 매수·매도 각각 0.05%, 슬리피지도 각각 0.05%. 가격은 봉 시가 기준가격이며 비용은 별도 반영했다. 최소 주문단위 반올림·역사적 호가·지연은 복원하지 않았다.', '',
        '## 매수·매도 내역 (KST)', '',
        '| 구분 | 날짜·시각 | 최초 수량 대비 | 기준가격 | 매도 후 현금 |',
        '|---|---|---:|---:|---:|',
        f"| 매수 | {ledger['entry']['time_kst']} | 100% | {ledger['entry']['reference_price_krw']:g}원 | 0원 |",
    ]
    for i,t in enumerate(ledger['sells'],1):
        report.append(f"| {i}차 매도 | {t['time_kst']} | {t['fraction_of_original_quantity']:.0%} | {t['reference_price_krw']:g}원 | {t['cumulative_cash_krw']:,.0f}원 |")
    report += ['',
        f"가중평균 매도 기준가격 **{ledger['weighted_exit_reference_krw']:g}원**. 비용 전 {ledger['gross_pct']:+.2f}%, 비용 반영 **{ledger['net_pct']:+.2f}%**. 최초 수량은 약 {ledger['entry']['quantity']:,.6f} ARK이며 각 매도는 그 절반이다.", '',
        '1차 매도 당시 누적 고점은 213원, 보호선은 202.35원이었다. 13일 10:55~11:00의 확정 5분봉 종가가 200원으로 보호선 아래여서 다음 봉 시가 200원을 사용했다. 보호선 가격에서 체결됐다고 가정하지 않았다.', '',
        '잔여분은 15일 일봉에서 세 지표가 모두 하락한 것을 16일 09:00에 확인하고, 그 시점 시가 198원에 청산했다. 15일 장중에는 해당 일봉의 마감 지표가 확정되지 않았다.', '',
        '## 기존 전량매도와 비교', '',
        '| 방식 | 최종 비용 반영 수익률 |', '|---|---:|',
        f"| A: 일봉 약화에 전량 매도 | {outcomes['A']['net_pct']:+.2f}% |",
        f"| C: 5% 추적 조건에 전량 매도 | {outcomes['C']['net_pct']:+.2f}% |",
        f"| 이번 50:50 분할매도 | {ledger['net_pct']:+.2f}% |", '',
        '이번처럼 동일 수량의 두 부분을 각각 기존 A/C 신호에 청산하고 재투자하지 않으면, 비용 반영 수익률도 두 방식의 가중평균이 된다. 분할 자체가 수익률을 높인다는 결론은 아니다.', '',
        '**15일 고가 253원 부근 매도 목표는 달성하지 못했다.** 14일 최고가 290원이나 15일 고가를 미리 알고 매도한 것으로 처리하지 않았다. 이번 조합은 일봉 확인의 지연과 빠른 추적매도의 조기 청산 문제를 모두 일부 남긴다.', '',
        '## 재현과 자료', '',
        '- `PYTHONPATH=. python research/ark_20260911/simulate_split.py`',
        '- [시나리오 조건](split_policy.json), [계산 결과·현금 원장](split_simulation.json), [계산 코드](simulate_split.py).',
        '- 기존 업비트 원자료: `research/september_exit_validation/data/KRW-ARK_5m.json.gz`, `daily_all.json.gz`.',
        '- 해당 보유기간의 일봉·5분봉 OHLCV 일치, 미래 데이터 제거 후 신호 불변, 수량·현금 합계, 일봉 청산 우선순위를 검증했다.',
        '- 기존 자료 수집 기록: https://github.com/Henryrotaewon/VPD-Investment/actions/runs/36163694576', '',
    ]
    (ROOT/'ARK_split_simulation.md').write_text('\n'.join(report))
    print(json.dumps(ledger, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
