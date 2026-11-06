"""Render whole-period trades, daily valuation and entry-filter discrepancies."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parent
KST=ZoneInfo('Asia/Seoul')
DAY=86400000


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000,KST).strftime('%m/%d %H:%M')


def main():
    r=json.loads((ROOT/'results.json').read_text())
    manifest=json.loads((ROOT/'data/manifest.json').read_text())
    s=r['scenarios'];main=s['indicator_pattern'];f=main['final']
    first=main['trades'][0]['reference'];fee=r['policy']['fee_per_side'];slip=r['policy']['slippage_per_side']
    hold=(f['price']/first*(1-slip)*(1-fee)/((1+slip)*(1+fee))-1)*100
    names={'strict_uniform':'기존 전체 필터를 최초부터 일관 적용',
           'assumed_initial_case':'12일 매수 가정 + 이후 기존 전체 필터',
           'indicator_pattern':'거래대금 필터만 제외하고 최초부터 일관 적용'}
    lines=['# 아크: 9월 11일부터 26일 02:30까지 전체 구간 시뮬레이션','',
        '대상은 업비트 ARK/KRW. 요청 시각은 2026년 9월 26일 02:32:24 KST이고, 마지막 완성 5분봉인 **26일 02:30**까지 계산했다. 11일은 일봉 시작인 09:00부터 감시한다. 매수·분할매도·최종 청산 이후의 재진입과 현금 대기까지 이어서 계산한 결과다. 운영 적용은 없다.','',
        f"**거래대금 필터를 제외한 지표 패턴 시나리오는 총 3회 진입, 기간 누적 {f['cumulative_pct']:+.2f}%**다. 300만원을 시작해 최종 현금 {f['cash']:,.0f}원, 실현손익 {f['realized_pnl']:+,.0f}원, 보유수량 {f['quantity']:g} ARK다. 앞선 +26.50%는 첫 거래의 결과였다.",'',
        '## 먼저 구분해야 할 진입 조건','',
        '이전 10종목 연구의 종목 선별 조건에 포함했던 “과거 20일 일 거래대금 중앙값 10억원 이상”을 그대로 적용하면 아크의 11일도 제외된다. 실제 11일의 해당 값은 약 3.93억원이었다. 따라서 이전의 12일 매수는 사용자가 지정한 사례 진입을 가정한 것이지 그 전체 필터가 생성한 신호가 아니었다.','',
        '이 충돌을 발견한 뒤, 거래대금 필터 하나만 제외하는 추가 시나리오를 기록하고 계산했다. 50:50 매도 비중과 지표·거래량 수치, 청산 조건은 유지했다. 이는 결과를 본 뒤의 진단용 비교이며 독립 표본 검증 또는 승인된 매매 기준이 아니다.','',
        '| 시나리오 | 매수 횟수 | 최종 누적 수익률 | 최종 현금 |',
        '|---|---:|---:|---:|']
    for key in ('strict_uniform','assumed_initial_case','indicator_pattern'):
        x=s[key];n=sum(t['side']=='BUY' for t in x['trades'])
        lines.append(f"| {names[key]} | {n} | {x['final']['cumulative_pct']:+.2f}% | {x['final']['cash']:,.0f}원 |")
    lines += ['',
        '아래 거래표는 세 번째 시나리오다. 처음부터 11일을 특별 대우하지 않고 동일한 지표 패턴을 적용했다.','',
        '## 전체 거래 내역 — 지표 패턴 시나리오','',
        '| 거래 | 매수일·시각 KST | 매수가 | 매도일·시각 KST 및 가격 | 해당 거래 순수익률 | 거래 종료 후 현금 |',
        '|---|---|---:|---|---:|---:|']
    for cycle in main['closed_cycles']:
        sells=[t for t in main['trades'] if t['cycle']==cycle['cycle'] and t['side']=='SELL']
        detail=' / '.join(f"{t['time_kst']} {t['reference']:g}원({t['original_quantity_fraction']:.0%})" for t in sells)
        lines.append(f"| {cycle['cycle']} | {clock(cycle['entry_ms'])} | {cycle['entry_reference']:g}원 | {detail} | {cycle['net_pct']:+.2f}% | {sells[-1]['cash_after']:,.0f}원 |")
    lines += ['',
        '**22일 09:00 청산 후 26일 02:30까지는 현금 대기**다. 22~24일의 일봉은 이전 거래량 급증·당일 거래량 감소 조건을 충족하지 않아 재진입하지 않았다. 25일 일봉은 아직 마감 전이므로 완성 일봉 신호로 사용하지 않았다. 매수 신호가 나온 14일은 기존 물량 보유 중이어서 추가 매수하지 않았다.','',
        f"최종 현금 {f['cash']:,.0f}원, 보유 평가액 0원, 미실현손익 0원이다. 누적 수익률 {f['cumulative_pct']:+.2f}%는 거래별 수익률을 단순 합산하지 않고, 매번 남은 현금을 재투자한 현금 원장으로 계산했다.",'',
        f"같은 12일 157원 진입 후 26일 02:30 기준가격 {f['price']:g}원까지 단순 보유하고 같은 청산 비용을 가정하면 **{hold:+.2f}%**다. 지표 패턴 시나리오는 22일 이후 상승에 재진입하지 못해 이 비교값보다 낮았다. 단순 보유의 값은 진단용이며 실제 매도 체결 또는 향후 보유 우월성을 뜻하지 않는다.",'',
        '## 고정한 매매·평가 가정','',
        '- 진입 지표: MACD 히스토그램 2차 차분 > 0, Wilder RSI14와 Williams14의 1차·2차 차분 > 0. 전일 거래량은 그 이전 20일 중앙값의 2배 이상이고, 신호일 거래량은 전일보다 작아야 한다. 연속 일봉 이력 150개 이상이 필요하다.','- 일봉이 확정된 다음날 09:00 이후 첫 실제 5분봉 시가를 진입 기준가격으로 사용한다. 과거 또는 보유 중 발생한 신호를 나중에 재사용하지 않는다.','- 보유 고점이 매수가 대비 +12%에 도달한 뒤, 확정 5분봉 종가가 누적 고점의 95% 이하이면 다음 실제 5분봉 시가에 최초 수량의 50%를 한 번 매도한다.','- MACD 히스토그램·RSI·Williams 중 두 개 이상이 하락한 일봉이 확정되면 남은 전량을 다음 실제 5분봉 시가에 매도한다. 일봉 청산이 먼저면 전량 청산하고, 같은 시각의 매도·재매수는 허용하지 않는다.','- 전량 청산 이후 새 확정 일봉 신호가 발생할 때만 남은 현금 전액으로 재진입한다. 분할매도 현금은 보유 중 재투자하지 않는다.','- 매수·매도 각각 수수료 0.05%, 슬리피지 0.05%. 소수 수량을 허용하며 실제 호가 잔량·지연·최소주문단위·세금은 모델링하지 않는다. 가격은 기준가격이고 비용은 현금 흐름에 별도 반영했다.','- 전체 구간 연구에서는 종전의 5일 비교 종료 청산을 제거했다. 마지막 보유분이 있다면 평가손익과 가상 청산 비용을 구분하며, 이번 세 시나리오는 모두 최종 보유분이 없다. 별도 손절·추가매수는 없다.','',
        '## 일별 평가 기록 — 지표 패턴 시나리오','',
        '완성 일봉은 다음날 09:00의 **다음 시가 주문 직전** 평가다. 예를 들어 15일 봉의 199원 종가 평가와 16일 09:00의 198원 매도는 서로 다르다. 마지막 행은 25일 진행 중 구간을 26일 02:30에 평가한 것이다. 미실현 물량에는 아직 발생하지 않은 매도 비용을 차감하지 않았다.','',
        '| 평가 대상 일봉/구간 | 평가 기준가격 | 현금 | 보유 ARK | 총 평가액 | 누적 수익률 |',
        '|---|---:|---:|---:|---:|---:|']
    for mark in main['daily_marks']:
        b=mark['evaluation_ms'];complete=b%DAY==0
        label=clock(b-DAY)[:5]+' 마감' if complete else clock(b)+' 현재'
        lines.append(f"| {label} | {mark['price']:g}원 | {mark['cash']:,.0f}원 | {mark['quantity']:,.3f} | {mark['equity']:,.0f}원 | {mark['cumulative_pct']:+.2f}% |")
    lines += ['',
        '## 매일의 포착 조건 점검','',
        '| 신호 대상 일봉 | 기존 전체 조건 | 거래대금 제외 지표 패턴 | 전체 패턴의 실제 처리 |',
        '|---|---|---|---|']
    actions={a['signal_day']:a['action'] for a in main['daily_actions']}
    action_names={'REENTRY_SIGNAL':'다음 일봉 시작 매수','IGNORE_ENTRY_WHILE_HOLDING':'보유 중이므로 추가 매수 없음',
                  'FULL_EXIT_SIGNAL':'다음 일봉 시작 전량 청산','NO_ENTRY_SIGNAL':'매수 신호 없음'}
    for a in r['audit']:
        yes=all(v for k,v in a['tests'].items() if k!='turnover')
        action=actions.get(a['signal_day'],'미처리')
        lines.append(f"| {a['signal_day']} | {'충족' if a['entry_ok'] else '미충족'} | {'충족' if yes else '미충족'} | {action_names.get(action,action)} |")
    lines += ['',
        '## 자료·검증·재현','',
        f"- 업비트 공개 자료 {manifest['requests']}회 조회: 완성 일봉 {manifest['daily_count']}개, 구간 5분봉 {manifest['minute_count']:,}개. 마지막 완성 일봉은 9월 24일이며 25일 진행 중 일봉을 신호 입력에서 제외했다.",
        '- 11~24일 14개 일자의 5분봉 OHLCV 합계를 독립 일봉과 대조했고, 앞서 저장한 아크 분봉과 겹치는 자료도 일치했다. 무거래 구간 247개는 가상 체결로 보충하지 않았다.',
        '- 미래 봉을 제거하고 체결 봉의 시가 이후 가격을 가려도 모든 체결 내역이 그대로 재현됐다. 기존 연구와 날짜가 겹치는 구간의 전체 진입 조건은 동일하게 재현됐다.',
        '- 재진입 시 현금 재투자, 부분 매도 후 잔고, 동일 경계의 전량 청산 우선순위, 수량·실현손익·평가액의 합계를 검증했다.',
        '- 자료 수집 기록: https://github.com/Henryrotaewon/VPD-Investment/actions/runs/36168055942 . 일회성 수집 워크플로는 완료 후 제거했다.',
        '- [원래 구간 조건](policy.json), [추가 진단 조건](pattern_sensitivity.json), [전체 계산 결과](results.json), [자료 목록](data/manifest.json). 압축 원자료는 data 디렉터리에 있다.',
        '- 재현: `PYTHONPATH=. python research/ark_period_20260911/replay.py` 후 `python research/ark_period_20260911/build_report.py`.',
        '',
        '이번 추가 비교는 조건 충돌을 드러내고 전체 구간의 실제 거래 순서를 보여주기 위한 것이다. 거래대금 필터 제거가 일반적으로 더 낫다는 결론은 아니며, 사용자가 합의한 종합 포착 점수·재진입 규칙을 대신하지 않는다. 운영 코드·텔레그램 시작 상태는 변경하지 않았다.','']
    (ROOT/'ARK_full_period_simulation.md').write_text('\n'.join(lines))


if __name__=='__main__':
    main()
