"""Render the archived five-asset replay without refitting any parameters."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    r=json.loads((ROOT/'results.json').read_text())
    manifest=json.loads((ROOT/'data/manifest.json').read_text())
    p=r['portfolio'];assets=r['assets']
    won=lambda x:f'{x:,.0f}원'
    pct=lambda x:f'{x:+.2f}%'
    price=lambda x:f'{x:,.6f}'.rstrip('0').rstrip('.')
    table=lambda header,rows:'\n'.join(['| '+' | '.join(header)+' |','| '+' | '.join(['---']*len(header))+' |',*['| '+' | '.join(map(str,row))+' |' for row in rows]])
    out=['# 5종목 지표가속 재진입·보유 시뮬레이션',
        '연구용 후보 규칙으로 과거를 재현한 결과다. 실제 모의계좌의 운영 실적이나 내일 수익률 예측이 아니다. 운영 코드·설정·배포는 변경하지 않았다.',
        '## 기준 시점과 자금',
        '- 거래소: 업비트 원화마켓. 아크(ARK), 비쓰리(B3), 스파크(SPK), 에테나(ENA), 에어로드롬파이낸스(AERO).\n'
        '- 기간: 2026-09-10 09:00부터 2026-09-26 02:55까지, 한국시간. 요청 시점 09-26 02:58:59 이전 마지막 완성 5분봉을 사용했다.\n'
        '- 총 3,000,000원, 종목당 600,000원. 시작은 전액 현금이며 최초 신호부터 매수한다. 종목별 손익을 그 종목 안에서 재투자하고 자금을 서로 옮기지 않는다.\n'
        '- 매수와 매도 각각 수수료 0.05%, 불리한 슬리피지 0.05%를 적용한다. 소수 수량을 허용한다.\n'
        '- 내일은 09-27로 해석하고 09:00 일봉 경계를 가정 청산시각으로 정했다. 해당 시세는 아직 없으므로 실제 최종 수익률을 계산할 수 없다.',
        '## 핵심 결과',
        f'현재 평가액은 **{won(p["marked_equity"])} ({pct(p["marked_return_pct"])})**다. 현재 가격으로 남은 전량을 청산한다는 비용 포함 가정은 **{won(p["liquidation_equity"])} ({pct(p["liquidation_return_pct"])})**, 최초 원금 대비 손익 **{won(p["liquidation_equity"]-p["initial"])}**다.',
        table(['종목','매수 횟수','기준시점 상태','마지막 가격','가정 청산 후 자산','누적 순수익률','5분 종가 기준 최대낙폭'],[
            [a['name'],sum(t['side']=='BUY' for t in a['trades']),'보유' if a['open_position'] else '현금',price(a['final']['price'])+'원',won(a['final']['liquidation_equity']),pct(a['final']['liquidation_return_pct']),f"{a['final']['marked_drawdown_pct']:.2f}%"] for a in assets.values()]),
        f'확정된 실현손익 합계는 **{won(p["realized_pnl"])}**, 미실현 평가손익은 **{won(p["unrealized_pnl"])}**, 현금은 **{won(p["cash"])}**다. 평가손익에는 아직 발생하지 않은 최종 매도 비용을 넣지 않았다. 포트폴리오의 5분 종가 평가액 기준 최대낙폭은 **{p["marked_drawdown_pct"]:.2f}%**다. 봉 안의 순간 낙폭은 더 클 수 있다.',
        '## 내일 청산 가격 시나리오',
        '기준시점 이후 추가 거래 없이 현재 보유한 네 종목을 계속 보유하고, 모두 같은 비율로 가격이 변한 뒤 청산한다고 가정한다. AERO는 이미 현금이므로 변하지 않는다. 전략을 실제 계속 운용하면 중간 청산·재진입 때문에 아래와 달라질 수 있다. **변동 0%는 비교 기준일 뿐 기대값이나 예측값이 아니다.**',
        table(['보유종목 가격변동','최종 자산','원금 대비 손익','누적 순수익률'],[
            [f'{float(k):+.0f}%',won(s['equity']),won(s['equity']-p['initial']),pct(s['return_pct'])] for k,s in p['tomorrow_scenarios'].items()]),
        '## 이번에 적용한 후보 규칙',
        '1. 직전 완성 일봉에서 RSI14와 Williams %R14가 모두 하락했어야 한다.\n'
        '2. 진행 중 일봉의 RSI와 Williams가 직전 일봉 값보다 높고, MACD(12,26,9) 히스토그램의 일간 2차 차분이 양수여야 한다. MACD 값 자체가 상승하거나 양수일 필요는 없다.\n'
        '3. 같은 일봉 안에서 연속 두 번의 정시 관측에서 조건을 확인한다. **일봉 지표를 1시간마다 갱신**하는 방식으로, 1시간봉 MACD를 쓰는 것이 아니다. 신호 확정 이후 실제 존재하는 다음 5분봉 시가에 비용을 적용해 매수한다. 이는 일봉 종가만 확인하고 다음 날 시가에 매수하는 버전과 다르다.\n'
        '4. 일봉당 최초 확인 신호만 유효하다. 보유 중인 신호는 버리고, 완전 청산 이후 새로 나온 신호로만 재진입한다. 같은 시각의 청산과 재매수를 허용하지 않는다.\n'
        '5. 추세청산 B: 완성 일봉의 MACD 히스토그램·RSI·Williams 중 두 개 이상이 전일보다 하락하면서 종가가 전일 저가보다 낮으면, 다음 실제 5분봉 시가에 전량 매도한다.\n'
        '6. 초기 보호선: 매수 신호 확인 당시까지 관측된 당일 저가로 고정한다. 이후 완성된 시간 구간의 종가가 이 가격을 밑돌면 다음 실제 5분봉 시가에 전량 매도한다. 보호선 가격 체결을 보장하지 않고 손절선을 낮추지도 않는다.\n'
        '7. 고정 익절률·보유기간 제한·부분 매도·거래량/거래대금 강제 필터·비트코인 시장 필터는 이번 후보에 없다. 거래량을 포함한 원래의 모든 지표 조건이 검증됐다는 뜻은 아니다.',
        '지표 간 값의 차이를 사용하며 0 또는 음수가 가능한 MACD·Williams를 단순 퍼센트 증감률로 나누지 않는다. RSI/Williams의 회복과 MACD 하락 둔화도 진입 후보가 된다. 이 규칙은 종합 수급을 직접 측정하는 모델이 아니다.',
        '## 전체 매매 기록',
        '아래 가격은 시세 원본의 체결 기준가격이다. 장부 수량·현금 계산에는 이 가격에 매수 +0.05%, 매도 −0.05%의 슬리피지와 각 0.05% 수수료를 별도로 적용했다. 모든 시각은 한국시간이다.']
    reasons={'FRESH_TWO_HOUR_DAILY_RECOVERY':'일봉 회복 2회 확인','DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK':'일봉 약화 + 전일 저가 이탈','HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE':'시간구간 종가가 초기 보호선 이탈'}
    for a in assets.values():
        out += ['### '+a['name'],table(['구분','일시','기준가격','신호 이후 체결봉 지연','실현손익','사유'],[
            ['매수' if t['side']=='BUY' else '매도',t['time_kst'],price(t['reference'])+'원',str(int(t['execution_bucket_delay_minutes']))+'분',won(t['realized_pnl']) if 'realized_pnl' in t else '—',reasons[t['reason']]] for t in a['trades']])]
        if a['open_position']:
            op=a['open_position']
            out.append(f"현재 남은 매수: {op['entry_kst']}, 기준가격 {price(op['entry_reference'])}원. 수량 {op['quantity']:,.8f}, 초기 보호선 {price(op['initial_stop'])}원. 내일 매도는 위 시나리오에만 존재하며 장부에는 실제 매도로 기록하지 않았다.")
    out += ['## 해석과 다음 검토 항목',
        '- 아크는 11일 21:00에 153원으로 진입하고, 18일 09:00에 190원으로 추세청산한 뒤 같은 날 19:00에 새 신호로 191원에 재진입했다. 이후 26일 02:55까지 유지된다. 사용자가 원했던 18일 재진입 후 보유 구간은 이 후보에서 재현된다. 다만 첫 구간은 14일 고점에 매도하지 못하며 종목별 최대낙폭이 33.70%다.\n'
        '- 에테나도 두 번째 진입 이후 상승을 유지했다. 전체 이익이 일부 종목과 미실현 수익에 집중돼 있다.\n'
        '- 에어로드롬은 7회 진입·7회 청산하면서 순손실 −18.52%였다. 초기 보호선이 가까운 진입과 반복 손절이 나타나므로 재진입의 질을 더 검토해야 한다. 결과를 본 뒤 이 종목만 제외하거나 규칙을 변경하지 않았다.\n'
        '- AERO 13일 11:25 체결 기준가격 768원은 신호 때의 보호선 769원보다 이미 낮다. 이번 고정 규칙에는 진입 직전 무효화 조건이 없어 그대로 매수된다. 보호선 아래로 갭이 난 진입의 취소, 보호폭 대비 변동성, 재진입 대기 조건은 **다음 연구 후보**이며 이 결과에 사후 반영하지 않았다.\n'
        '- ARK를 보고 규칙을 개발했고 종목도 사용자가 선택했다. 이 결과는 독립적인 전략 검증이 아니다. 실전 수준을 판단하려면 다른 기간·종목에서 사전에 고정한 규칙을 검증해야 한다.',
        '## 준비기간 변경 공개 및 민감도',
        '처음 저장한 정책에는 완성 일봉 150개 이상을 요구했다. 수집 단계에서 9월 10일 이전 B3는 126개, SPK는 140개여서 조건을 충족하지 못했다. **다섯 종목 수익률을 계산하기 전에** 기존 지표 모듈의 최소 준비기간인 100일을 공통 적용하는 수정안을 별도 기록했다. 지표 계산에는 확보된 전체 과거 자료를 쓰며 100개로 자르지 않는다. 이 조치가 초기화 오차가 없음을 보장하는 것은 아니다.',
        table(['종목','시작 전 완성 일봉','100일 조건 매수/순수익률','원래 150일 조건 매수/순수익률'],[
            [assets[m['market']]['name'],m['completed_history_before_start'],f"{sum(t['side']=='BUY' for t in assets[m['market']]['trades'])}회 / {pct(assets[m['market']]['final']['liquidation_return_pct'])}",f"{assets[m['market']]['original_150_day_sensitivity']['entry_count']}회 / {pct(assets[m['market']]['original_150_day_sensitivity']['final']['liquidation_return_pct'])}"] for m in manifest['markets']]),
        f"150일 조건을 유지한 포트폴리오의 현재가 가정 청산 순수익률은 **{pct(p['original_150_day_sensitivity']['liquidation_return_pct'])}**다. B3는 거래하지 못하고 SPK는 후반 진입부터 허용된다. 이는 준비기간에 따른 진입 자격 민감도이며 최적 준비기간을 고른 결과가 아니다.",
        '## 일별 평가 기록',
        '일봉 경계인 매일 09:00 직전까지의 관측가격으로 평가한다. 해당 09:00 시가 주문은 다음 5분 기록에 반영된다. 마지막 행만 요청 기준시점이다. 매도되지 않은 보유분에는 미래 매도 비용을 차감하지 않았다.',
        table(['시점','전체 평가액','누적 평가수익률'],[['09/10 09:00',won(p['initial']),'+0.00%'],*[[d['time_kst'],won(d['equity']),pct(d['return_pct'])] for d in r['daily_marks']]]),
        '## 자료와 검증',
        table(['마켓','완성 일봉 수','5분봉 수','5분 구간 중 원본 봉 없음'],[[m['market'],m['daily_count'],m['minute_count'],m['missing_buckets']] for m in manifest['markets']]),
        '업비트 공개 API에서 해당 기준시점 이전 자료를 수집했다. 모든 종목의 마지막 5분봉 종가는 09/26 02:55 기준이다. 원본에 없는 구간은 주문 체결에 사용하지 않았고, 평가할 때만 최근 관측가격을 이어 썼다. SPK와 AERO의 빈 구간이 많고 일부 주문은 다음 원본 봉까지 25분 지연된다. 봉이 존재해도 전량이 해당 시가에 체결된다는 보장은 없다.',
        '- 5종목 × 15일 = 75개 일자의 5분봉 집계 OHLCV를 독립 일봉 자료와 대조했다.\n'
        '- 총 30건의 매수·매도에서 미래 봉을 제거하고 체결봉의 시가 이외 정보를 가렸을 때 이전 거래가 동일한지 확인했다. 각 종목 최초 확인 신호의 지표도 미래 일봉·분봉을 제거해 재계산했다.\n'
        '- 과거 ARK 연구 원본과 겹치는 5분봉은 모두 일치한다.\n'
        '- 현금·수량·실현/미실현 손익의 보존, 재투자, 청산 우선순위, 동일 시점 재매수 차단, 손절 갭 체결을 확인했다.\n'
        '- 실제 호가 깊이·체결 지연·최소 주문·수량 반올림·세금은 모델링하지 않았다. 고정 0.05% 슬리피지가 실제 체결비용을 대표한다고 단정할 수 없다.',
        '[공개자료 수집 실행](https://github.com/Henryrotaewon/VPD-Investment/actions/runs/36171610653) · [연구 초안 PR #59](https://github.com/Henryrotaewon/VPD-Investment/pull/59)',
        '재현: 저장소 루트에서 `PYTHONPATH=. python research/five_asset_reentry_20260910/replay.py`, 이어서 `python research/five_asset_reentry_20260910/build_report.py`. 원본은 `data/`, 원래 정책은 `policy.json`, 준비기간 수정은 `warmup_revision.json`, 정밀 수치·장부·신호 처리 기록은 `results.json`에 있다.']
    (ROOT/'FIVE_ASSET_reentry_simulation.md').write_text('\n\n'.join(out)+'\n')


if __name__=='__main__':
    main()
