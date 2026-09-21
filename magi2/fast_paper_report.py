"""Read-only current FAST paper balances and KST daily results."""
from datetime import datetime
from magi2.fast_paper import KST

NAMES = dict(upbit='업비트',bithumb='빗썸',binance='바이낸스',kraken='크라켄')
REASONS = {'HOLD_5M':'5분 시장가 청산','DEADLINE_10M':'10분 강제청산',
           'SESSION_10M':'10분 반복 종료','LIMIT_UNFILLED_10M':'10분 매수 미체결',None:'보유 중'}


def tick(t):
    return t.get('execution_version')=='fast-tick-cycle-v3'


def sessions(ledger,now_ms):
    return getattr(ledger,'recent_sessions',ledger.history)(now_ms,10)


def return_pct(t):
    basis=t['budget_quote'] if tick(t) else t['entry_cost']
    return t['realized_quote']/basis*100 if basis else 0.


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
            '각 거래소 최초 100만원 · 종목당 20만원 · 최대 5종목',
            'v3 현재가−1틱 매수/+1틱 매도 반복 · 포착 후 10분 잔량 시장가',
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
        rows.append(f'매수 체결 {a["bought"]} · 세션 완료 {a["closed"]} · 양수 {a["wins"]} · 제외/미체결 {a["skipped"]}')
        rows.append(f'현재 진행 {len(a["active"])} · 10분 초과 미청산 {a["overdue"]} · 집계일 지연 청산 {a["late_closed"]}')
        for version,c in a.get('cohorts',{}).items():
            label=('v3 1틱 반복' if version=='fast-tick-cycle-v3' else
                   'v2 거래량·가속/5분 보유' if version=='fast-volume-accel-v2' else 'v1 매수세/5분 보유')
            rows.append(f'{label} · 완료 {c["closed"]}건 전체 순손익 {money(c["closed_trade_pnl_krw"],True)}')
            if version=='fast-tick-cycle-v3':
                rows.append(f'지정가 왕복 {c["cycles"]}회 · 비용 전 {money(c["gross_krw"],True)} · 수수료 {money(c["fees_krw"])}')
                rows.append(f'추가 슬리피지 {money(c["slippage_krw"])} · 강제청산 순손익 {money(c["forced_pnl_krw"],True)} (전체에 포함)')
        if a['unknown_marks']:
            rows.append(f'호가 미확인 {a["unknown_marks"]}건 · 전체 자산평가 보류')
        if a['error']:
            rows.append('시세 조회 오류: '+a['error'][:80])
        if a['heartbeat_ms'] is None or data['now_ms']-a['heartbeat_ms']>30_000:
            rows.append('모의 감시 응답 확인 필요')
        rows.append('')
    rows += ['해외 자금은 최초 공개시세로 환산 후 환산율 고정(환율손익 제외).',
             'v3 메이커/테이커 수수료 가정: 업비트 0.05/0.05%, 빗썸 0.04/0.04%, 바이낸스 0.1/0.1%, 크라켄 0.4/0.8%.',
             '지정가: 대기 물량·실제 체결량 모형. 강제청산: bid 깊이+추가 슬리피지 0.05%.',
             '구버전 비용 유지. 자료 단절·잔량/최소주문 미달은 청산 지연 표시.',
             '모의투자 · 실제 주문 없음 · 매일 09:00 KST 전일 보고']
    return '\n'.join(rows)


def recent(ledger, now_ms):
    rows = ['📊 FAST 모의검증 결과 · 최근 10건',
            'v3 1틱 반복 · 포착→종료 KST · 순수익률은 배정 20만원 기준', '']
    trades = sessions(ledger,now_ms)
    for t in trades:
        if tick(t):
            timing=clock(t['signal_ms'])+'→'+(clock(t['close_ms']) if t.get('close_ms') else '진행')
            if t['status']=='CLOSED':result=f'{return_pct(t):+.2f}%'
            elif t['status']=='SKIPPED':result='매수 미체결 · 0.00%'
            elif t['status']=='EXIT_PENDING':result='잔량 청산 대기'
            else:result='매수 대기' if not t.get('entry_ms') else '반복 중'
            result+=f' · 왕복 {t["cycles_completed"]}회'
            if t.get('forced_exits'):result+=' · 마지막 시장가'
            if t['status']=='EXIT_PENDING' and now_ms>t['deadline_ms']:result+=' · 10분 초과'
            rows.append(f'{NAMES[t["venue"]]} {t["symbol"]} v3 | {timing} | {result}')
            continue
        entry = clock(t['entry_ms'])
        if t['status'] == 'CLOSED':
            timing = entry+'→'+clock(t['close_ms'])
            result = f'{return_pct(t):+.2f}%'
            if t.get('deadline_delay_ms', 0):
                result += ' · 지연 청산'
        else:
            timing = entry+'→미청산'
            result = '청산 대기' if t['status'] == 'EXIT_PENDING' else '보유 중'
            if now_ms > t['deadline_ms']:
                result += ' · 10분 초과'
        version='v2' if t.get('strategy_version')=='fast-volume-accel-v2' else 'v1'
        rows.append(f'{NAMES[t["venue"]]} {t["symbol"]} {version}·5분 보유 | {timing} | {result}')
    if not trades:
        rows.append('아직 모의 매수 내역이 없습니다. 신규 조건 충족 포착부터 기록합니다.')
    rows.append('\n모의투자 · 실제 주문 없음')
    return '\n'.join(rows)


def history(ledger, now_ms):
    rows = ['📒 FAST 모의 거래내역 · 최근 10건 · KST']
    for t in sessions(ledger,now_ms):
        fx = ledger.account(t['venue'])['fx_krw_per_quote']
        if tick(t):
            rows += [f'\n{NAMES[t["venue"]]} {t["symbol"]} v3 · {t["status"]}',
                     f'포착 {clock(t["signal_ms"])} · 기한 {clock(t["deadline_ms"])}',
                     '첫 매수 '+(clock(t['entry_ms']) if t.get('entry_ms') else '미체결')+
                     ' · 마지막 매도 '+(clock(t['last_sell_ms']) if t.get('last_sell_ms') else '없음'),
                     f'왕복 {t["cycles_completed"]}회 · 실현 {money(t["realized_quote"]*fx,True)} ({return_pct(t):+.2f}%/배정금)',
                     f'수수료 {money(t["fees_paid"]*fx)} · 강제청산 {money(t["forced_exit_pnl"]*fx,True)} (포함)']
            if t.get('order'):
                o=t['order'];rows.append(f'{"매수" if o["side"]=="BUY" else "매도"} 지정가 {o["price"]:g} · 미체결 {o["remaining"]:g}')
            if t.get('last_error'):rows.append('대기 사유: '+t['last_error'])
            continue
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
