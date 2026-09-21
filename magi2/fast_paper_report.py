"""Read-only current FAST paper balances and KST daily results."""
from datetime import datetime
from magi2.fast_paper import KST

NAMES = dict(upbit='업비트',bithumb='빗썸',binance='바이낸스',kraken='크라켄')
REASONS = {'HOLD_5M':'5분 시장가 청산','DEADLINE_10M':'10분 강제청산',None:'보유 중'}


def clock(ms):
    return datetime.fromtimestamp(ms/1000,KST).strftime('%m/%d %H:%M:%S')


def money(value, signed=False):
    if value is None:
        return '평가 대기'
    return (f'{value:+,.0f}' if signed else f'{value:,.0f}')+'원'


def keyboard():
    return {'inline_keyboard':[
        [{'text':'📊 FAST 모의검증 결과','callback_data':'nav:fast_report'},
         {'text':'📅 전일 결과','callback_data':'nav:fast_daily'}],
        [{'text':'💰 거래소별 잔고','callback_data':'nav:fast_balance'},
         {'text':'📒 거래 상세','callback_data':'nav:fast_orders'}],
        [{'text':'⚡ 포착 목록','callback_data':'nav:fast'}],
        [{'text':'🧪 과거 재생검증','callback_data':'nav:fast_replay'},
         {'text':'↩️ 메인 메뉴','callback_data':'nav:menu'}]]}


def summary(data, daily=False):
    title = '📅 FAST 일일 모의투자 결과' if daily else '📊 FAST 모의투자'
    rows = [title, f'집계 {data["date"]} KST · '+('전일 청산 실현손익' if daily else '오늘 청산 실현손익'),
            '각 거래소 최초 100만원 · 회당 20만원 · 최대 5종목',
            '기본 5분 시장가 청산 · 포착 후 10분 강제청산',
            f'현재 잔고·평가 기준 {clock(data["now_ms"])} KST','']
    for a in data['accounts']:
        rows.append(f'{NAMES[a["venue"]]} · {a["quote"]}')
        if a['funded_ms'] is None:
            rows.append('초기 100만원 환산 대기 · 신규 매수 보류')
        else:
            rows.append(f'현금 {money(a["cash_krw"])} · 평가자산 {money(a["equity_krw"])}')
            if a['equity_krw'] is not None:
                rows.append(f'원금 대비 누적 {(a["equity_krw"]/a["initial_krw"]-1)*100:+.2f}%')
            rows.append(f'집계일 실현 {money(a["pnl_krw"],True)} · 수수료 {money(a["fees_krw"])}')
        rows.append(f'매수 {a["bought"]} · 청산 완료 {a["closed"]} · 양수 거래 {a["wins"]} · 진입 제외 {a["skipped"]}')
        rows.append(f'현재 진행 {len(a["active"])} · 10분 초과 미청산 {a["overdue"]} · 집계일 지연 청산 {a["late_closed"]}')
        if a['unknown_marks']:
            rows.append(f'호가 미확인 {a["unknown_marks"]}건 · 전체 자산평가 보류')
        if a['error']:
            rows.append('시세 조회 오류: '+a['error'][:80])
        if a['heartbeat_ms'] is None or data['now_ms']-a['heartbeat_ms']>30_000:
            rows.append('모의 감시 응답 확인 필요')
        rows.append('')
    rows += ['해외 자금은 최초 공개시세로 환산 후 환산율 고정(환율손익 제외).',
             '편도 수수료 가정: 업비트 0.05% · 빗썸 0.04% · 바이낸스 0.1% · 크라켄 0.4%.',
             '매수 ask·매도 bid 깊이와 편도 추가 슬리피지 0.05% 반영.',
             '호가 단절·잔량 부족 시 청산 대기·지연을 표시하며 체결을 추정하지 않습니다.',
             '모의투자 · 실제 주문 없음 · 매일 09:00 KST 전일 보고']
    return '\n'.join(rows)


def recent(ledger, now_ms):
    rows = ['📊 FAST 모의검증 결과 · 최근 10건', '매수→매도 시각 (KST) · 비용 반영 순수익률', '']
    trades = ledger.history(now_ms, 10)
    for t in trades:
        entry = clock(t['entry_ms'])
        if t['status'] == 'CLOSED':
            timing = entry+'→'+clock(t['close_ms'])
            result = f'{t["realized_quote"]/t["entry_cost"]*100:+.2f}%'
            if t.get('deadline_delay_ms', 0):
                result += ' · 지연 청산'
        else:
            timing = entry+'→미청산'
            result = '청산 대기' if t['status'] == 'EXIT_PENDING' else '보유 중'
            if now_ms > t['deadline_ms']:
                result += ' · 10분 초과'
        rows.append(f'{NAMES[t["venue"]]} {t["symbol"]} | {timing} | {result}')
    if not trades:
        rows.append('아직 모의 매수 내역이 없습니다. 신규 강한 포착부터 기록합니다.')
    rows.append('\n모의투자 · 실제 주문 없음')
    return '\n'.join(rows)


def history(ledger, now_ms):
    rows = ['📒 FAST 모의 거래내역 · 최근 진입 10건']
    for t in ledger.history(now_ms):
        fx = ledger.account(t['venue'])['fx_krw_per_quote']
        rows += [f'\n{NAMES[t["venue"]]} {t["symbol"]} · {t["status"]}',
                 f'매수 {clock(t["entry_ms"])} · 원가 {money(t["entry_cost"]*fx)}']
        if t['status']=='CLOSED':
            rows += [f'매도 {clock(t["close_ms"])} · {REASONS.get(t["reason"],t["reason"])}',
                     f'순손익 {money(t["realized_quote"]*fx,True)} ({t["realized_quote"]/t["entry_cost"]*100:+.2f}%) · 보유 {(t["close_ms"]-t["entry_ms"])/1000:.0f}초']
            if t.get('deadline_delay_ms',0)>0:
                rows.append(f'10분 기한 초과 {t["deadline_delay_ms"]/1000:.0f}초')
        else:
            rows.append(f'청산 기한 {clock(t["deadline_ms"])} KST · '+REASONS.get(t['reason'],t['reason'] or '보유 중'))
            if t['last_error']:
                rows.append('미청산 사유: '+t['last_error'])
    if len(rows)==1:
        rows.append('아직 모의 매수 내역이 없습니다. 신규 강한 포착부터 기록합니다.')
    rows.append('\n모든 거래는 가상 체결이며 실제 주문이 아닙니다.')
    return '\n'.join(rows)


def view(ledger, now_ms, kind='fast_report'):
    if ledger is None:
        return 'FAST 모의원장 준비 중입니다.', keyboard()
    if kind=='fast_report':
        return recent(ledger,now_ms), keyboard()
    if kind=='fast_orders':
        return history(ledger,now_ms), keyboard()
    date = None
    if kind=='fast_daily':
        from datetime import timedelta
        date = (datetime.fromtimestamp(now_ms/1000,KST).date()-timedelta(days=1)).isoformat()
        if date < datetime.fromtimestamp(ledger.started_ms/1000,KST).date().isoformat():
            return '📅 FAST 전일 결과\n모의투자 시작 전 날짜입니다. 첫 일일 보고는 시작일 다음 날 09:00 KST입니다.', keyboard()
    return summary(ledger.snapshot(now_ms,date),daily=kind=='fast_daily'), keyboard()
