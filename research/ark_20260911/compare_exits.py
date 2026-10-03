"""Conditional ARK daily-exit comparison. Archived inputs only; no orders.

Rules are frozen before evaluation: A = at least two negative daily first
differences among MACD histogram/RSI/Williams; B = A AND close < prior low.
No calibration or optimization; the entry is supplied as a case-study premise.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from magi2.daily_indicator_review import review
from magi2.fast_wave_indicator import Candle, DAY_MS

ROOT = Path(__file__).resolve().parent
COMPONENTS = ('macd_hist', 'rsi', 'williams')
START, END = '2026-09-12', '2026-09-15'
EPSILON = 1e-10  # Floating-point tolerance, NOT a fitted signal threshold.
FEE, SLIPPAGE = 0.0005, 0.0005  # Research scenario: 0.05% per side each.


def stamp(row):
    return int(datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc).timestamp()*1000)


def candle(row):
    return Candle(stamp(row), row[2], row[3], row[4], row[5])


def kst(ms):
    return datetime.fromtimestamp(ms/1000, ZoneInfo('Asia/Seoul')).isoformat()


def returns(entry, exit_reference):
    ratio = exit_reference/entry
    # Equal all-in initial cash; no volume/depth/latency reconstruction.
    net_ratio = ratio*(1-SLIPPAGE)*(1-FEE)/((1+SLIPPAGE)*(1+FEE))
    return dict(gross_pct=(ratio-1)*100, cost_scenario_net_pct=(net_ratio-1)*100)


def main():
    original = json.loads((ROOT/'public_candles.json').read_text())
    supplement = json.loads((ROOT/'exit_reference.json').read_text())
    assert original['columns'] == supplement['columns']
    original_rows = original['training'] + original['outcomes']
    by_date = {r[0]: r for r in original_rows}
    overlap = 0
    for row in supplement['candles']:
        if row[0] in by_date:
            assert row == by_date[row[0]], 'ARCHIVED_CANDLES_DISAGREE'
            overlap += 1
        else:
            by_date[row[0]] = row
    assert overlap == 2
    raw = [by_date[k] for k in sorted(by_date)]
    bars = [candle(r) for r in raw]
    assert all(b.open_ms-a.open_ms == DAY_MS for a,b in zip(bars,bars[1:]))
    for row, bar in zip(raw, bars):
        bar.validate()
        assert bar.low <= row[1] <= bar.high

    entry_row = next(r for r in raw if r[0][:10] == START)
    entry = entry_row[1]
    days = []
    for i, row in enumerate(raw):
        if not START <= row[0][:10] <= END:
            continue
        available = bars[i].open_ms + DAY_MS
        features = review(bars[:i+1], available, venue='upbit', symbol='KRW-ARK')
        # Complete future bars, including Sep 16 outcomes, cannot change signals.
        assert review(bars, available, venue='upbit', symbol='KRW-ARK') == features
        levels = features['observations'][-1]['values']
        changes = features['changes']
        falling = [k for k in COMPONENTS if changes[k]['velocity'] < -EPSILON]
        decelerating = [k for k in COMPONENTS if changes[k]['acceleration'] < -EPSILON]
        price_break = row[4] < raw[i-1][3]
        a_signal = len(falling) >= 2
        b_signal = a_signal and price_break
        assert raw[i+1][0][:10] <= '2026-09-16'
        assert bars[i+1].open_ms == available
        days.append(dict(
            date=row[0][:10], signal_available_kst=kst(available),
            open=row[1], high=row[2], low=row[3], close=row[4], prior_low=raw[i-1][3],
            volume=row[5], volume_change_pct=(row[5]/raw[i-1][5]-1)*100,
            values={k:levels[k] for k in COMPONENTS},
            changes={k:changes[k] for k in COMPONENTS},
            falling=falling, decelerating=decelerating, price_break=price_break,
            rule_a_signal=a_signal, rule_b_signal=b_signal,
            next_open_reference=raw[i+1][1],
            close_reference_returns=returns(entry,row[4])))

    a_first = next((d for d in days if d['rule_a_signal']), None)
    b_first = next((d for d in days if d['rule_b_signal']), None)
    assert a_first['date'] == END and b_first is None
    endpoint = bars[-1].open_ms
    assert raw[-1][0][:10] == '2026-09-16'
    endpoint_price = raw[-1][1]
    outcomes = {
        'A_indicator_decline': dict(status='EXIT_AT_NEXT_OPEN_REFERENCE',
            signal_day=a_first['date'], exit_reference_kst=a_first['signal_available_kst'],
            exit_reference_price=a_first['next_open_reference'],
            **returns(entry,a_first['next_open_reference'])),
        'B_decline_and_price_break': dict(status='OPEN_AT_ENDPOINT', signal_day=None,
            mark_kst=kst(endpoint), mark_reference_price=endpoint_price,
            **returns(entry,endpoint_price)),
    }
    assert math.isclose(outcomes['A_indicator_decline']['gross_pct'],
                        outcomes['B_decline_and_price_break']['gross_pct'])
    peak = max(days, key=lambda d:d['high'])
    result = dict(
        mode='RESEARCH_ONLY_NOT_RUNTIME_PAPER', market='KRW-ARK',
        entry=dict(date=START, reference_kst=kst(stamp(entry_row)), reference_price=entry,
                   assumed=True, approved_entry_rule=False),
        signal_window=[START,END], endpoint_kst=kst(endpoint),
        rules=dict(A='At least 2 of 3 daily first differences < 0',
                   B='A AND close strictly below previous daily low',
                   components=list(COMPONENTS), epsilon=EPSILON,
                   intraday_protection=False, other_tp_sl=False,
                   normalization_threshold=None, volume_is_exit_gate=False),
        execution_model='NEXT_OPEN_REFERENCE_WITH_FIXED_COST_SCENARIO_NOT_ACTUAL_FILLS',
        costs=dict(fee_per_side=FEE, slippage_per_side=SLIPPAGE,
                   authenticated_fee=False, historical_depth_available=False),
        days=days, outcomes=outcomes,
        end_of_sep15=dict(price=days[-1]['close'], **returns(entry,days[-1]['close']),
                         a_status='EXIT_SIGNAL_PENDING_NEXT_SESSION', b_status='OPEN'),
        peak_reference=dict(date=peak['date'], price=peak['high'],
            gross_gain_pct=(peak['high']/entry-1)*100,
            peak_to_endpoint_price_change_pct=(endpoint_price/peak['high']-1)*100,
            is_executable_exit=False, exact_intraday_mdd_known=False),
        checks=dict(future_excluded=True, contiguous_history=True,
                    overlap_days_identical=overlap, same_endpoint_comparison=True),
        caveats=['Conditional entry, not a validated signal strategy.',
                 'One selected case cannot establish profitability or generalize exit quality.',
                 'B remains open; no terminal return or winner is established.',
                 'Daily OHLC cannot reconstruct order depth, latency or intraday stop fills.',
                 'No per-indicator materiality threshold has been calibrated.',
                 'MACD/RSI/Williams are correlated price-derived measurements.'])
    (ROOT/'exit_comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')

    a = outcomes['A_indicator_decline']; b = outcomes['B_decline_and_price_break']
    text = [
        '# 지표가속 — 아크 9월 12~15일 추세청산 비교', '',
        '지표 하락만 보는 A안은 15일 봉에서 청산 신호가 발생한다. 신호 확인 시점은 '
        '16일 09:00 KST이며, 이때 시가 기준가격은 198원이다. 가격 이탈까지 확인하는 '
        'B안은 15일 봉까지 청산 신호가 없다. 같은 시점의 가격 평가는 같으므로 '
        '이번 구간만으로 최종 수익률 우열을 판정할 수 없다.', '',
        '## 비교 조건', '',
        '- 공통 진입: 9월 12일 09:00 KST 시가 157원을 기준가격으로 가정. '
        '확정·검증된 자동 매수 신호나 실제 체결이 아니다.',
        '- A: MACD 히스토그램·Wilder RSI14·Williams %R14 중 두 개 이상의 '
        '일별 변화량이 음수면, 신호 봉 마감 후 다음 일봉 시작에 청산.',
        '- B: A 조건과 함께 종가가 전일 저가보다 낮아야 청산. 동일한 날짜에 두 조건이 모두 필요하다.',
        '- 가속도 음수는 상승폭 둔화를 뜻할 수 있으므로 A의 하락 조건과 구분한다. '
        '거래량·별도 익절/손절·장중 보호 조건은 이번 비교의 청산 조건에 넣지 않았다.',
        '- 유의미한 하락폭 기준은 미확정이므로 부호만 비교한다. 1e-10은 부동소수점 '
        '오차 방지용이며 최적화한 매매 임계값이 아니다.',
        '- 매수·매도 각각 수수료 0.05%와 슬리피지 0.05%는 별도 비용 시나리오. '
        '실계좌 수수료나 역사적 호가로 검증한 체결 비용이 아니다.', '',
        '## 날짜별 지표와 판단', '',
        '표의 지표는 해당 일봉 마감 후에만 확정된다. 화살표는 전일 대비 방향이다.', '',
        '| 일봉 | 종가 | MACD 히스토그램 | RSI14 | Williams %R14 | 하락 지표 | 전일 저가 | 종가 이탈 | A / B |',
        '|---|---:|---:|---:|---:|---:|---:|---|---|',
    ]
    for d in days:
        cells = [f'{d["values"][k]:.4f} '+('↓' if k in d['falling'] else '↑') for k in COMPONENTS]
        text.append(f'| {d["date"][5:]} | {d["close"]:g}원 | '+ ' | '.join(cells)+
                    f' | {len(d["falling"])}/3 | {d["prior_low"]:g}원 | '+
                    ('예' if d['price_break'] else '아니오')+' | '+
                    ('청산 신호' if d['rule_a_signal'] else '보유')+' / '+
                    ('청산 신호' if d['rule_b_signal'] else '보유')+' |')
    text += ['',
        '13일에는 세 지표의 가속도가 모두 음수지만, 값 자체가 하락한 것은 Williams '
        '하나뿐이다. MACD와 RSI는 상승했고 14일에는 세 지표가 모두 다시 상승했다. '
        '가속도 둔화와 방향 하락을 구분해야 하는 사례다.', '',
        '15일에는 세 지표가 모두 하락한다. 하지만 종가 199원은 전일 저가 182원보다 '
        '높아 B안은 계속 보유한다.', '',
        '## 같은 시점에서 비교한 결과', '',
        '평가 시점: 9월 16일 09:00 KST. A는 청산 기준가격, B는 보유 평가 기준가격이다.', '',
        '| 항목 | A: 지표 하락 | B: 지표 하락 + 전일 저가 이탈 |',
        '|---|---|---|',
        '| 최초 청산 신호 | 9월 15일 봉 | 9월 15일 봉까지 없음 |',
        '| 9월 16일 09:00 상태 | 청산 가정 | 보유 |',
        f'| 매도/평가 기준가격 | {endpoint_price:g}원 | {endpoint_price:g}원 |',
        f'| 비용 전 가격 수익률 | {a["gross_pct"]:+.4f}% | {b["gross_pct"]:+.4f}% 평가 |',
        f'| 비용 시나리오 수익률 | {a["cost_scenario_net_pct"]:+.4f}% 청산 가정 | '
        f'{b["cost_scenario_net_pct"]:+.4f}% 청산 비용까지 가정한 평가 |', '',
        '비용 시나리오 계산: `(매도/매수가) × (1−매도슬리피지) × (1−매도수수료) '
        '/ [(1＋매수슬리피지) × (1＋매수수수료)] − 1`.',
        f'15일 종가 199원에서 두 안의 비용 전 평가는 모두 '
        f'{result["end_of_sep15"]["gross_pct"]:+.4f}%지만, '
        'A의 청산 신호로 같은 봉 종가에 미리 팔았다고 처리하지 않는다. '
        '16일 시가와 지연·호가를 구분하며, 이 연구에는 지연·잔량 자료가 없다.', '',
        '## 해석', '',
        f'- 14일 고가 290원은 진입 기준가격 대비 {result["peak_reference"]["gross_gain_pct"]:+.2f}%였다. '
        f'16일 시가 198원은 그 고가보다 {abs(result["peak_reference"]["peak_to_endpoint_price_change_pct"]):.2f}% 낮다. '
        '고가 매도 가능성을 가정하거나 정확한 장중 최대낙폭을 계산한 것은 아니다.',
        '- A도 급등 중 되돌림을 빠르게 막지는 못했다. B는 가격 확인 조건 때문에 '
        '더 오래 보유할 수 있다. 이후 회복 또는 추가 하락 여부에 따라 유불리가 달라진다.',
        '- 일봉 추세청산과 장중 수익 보호를 별도로 검증할 필요가 있다. 일봉만으로 '
        '장중 트레일링 체결 가격·순서를 복원하면 안 된다.',
        '- 아크 한 사례로 임계값을 맞추지 않고 다른 종목·날짜에도 동일 기준을 적용해야 한다.',
        '- 운영 코드·매매 설정·텔레그램 시작 상태는 변경하지 않았다.', '',
        '## 원자료와 재현', '',
        '- [원본 400일 + 결과 4일](public_candles.json)',
        '- [추가 조회: 14~16일](exit_reference.json): 14·15일 원본과 전 필드 일치. '
        '16일에는 시가만 체결/평가 기준으로 사용하며, 그날 고가·저가·종가·거래량은 신호에 넣지 않음.',
        '- [계산 결과](exit_comparison.json), [계산 코드](compare_exits.py)',
        '- 재현: 저장소 루트에서 `PYTHONPATH=. python research/ark_20260911/compare_exits.py`',
        '- [공개 API 추가 수집 기록]('+supplement['collection_run']+')',
        '- [업비트 일봉 API](https://docs.upbit.com/kr/reference/list-candles-days)',
        '- 검증: 일봉 연속성·OHLC 범위·중복 자료 일치·미래 봉 제외·동일 평가시점 확인.', '',
    ]
    (ROOT/'ARK_exit_comparison.md').write_text('\n'.join(text))
    print(json.dumps(dict(outcomes=outcomes,days=[dict(date=d['date'],falling=d['falling'],
                     a=d['rule_a_signal'],b=d['rule_b_signal']) for d in days],
                     peak=result['peak_reference'],checks=result['checks']),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
