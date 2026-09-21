"""Read-only current FAST paper balances and KST daily results."""
from datetime import datetime
from magi2.fast_paper import KST

NAMES = dict(upbit='업비트',bithumb='빗썸',binance='바이낸스',kraken='크라켄')
REASONS = {'HOLD_5M':'5분 시장가 청산','DEADLINE_10M':'10분 강제청산',
           'SESSION_10M':'10분 반복 종료','LIMIT_UNFILLED_10M':'10분 매수 미체결','TAKE_PROFIT_12':'+12% 익절','STOP_LOSS_6':'−6% 손절','MANUAL_FAST_CLEAR':'FAST 정리',None:'보유 중'}


def tick(t):
    return t.get('execution_version') in ('fast-tick-cycle-v3','fast-current-cycle-v4','fast-target-v5')


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


def unit_price(value, quote):
    number = f'{value:,.12f}'.rstrip('0').rstrip('.')
    return number+('원' if quote=='KRW' else ' '+quote)


def position_lines(t, account, now_ms):
    lines=[f'{NAMES[t["venue"]]} · {t["symbol"]}', f'포착일시 {clock(t["signal_ms"])} KST']
    if t['status']=='ENTRY_PENDING' and not t.get('entry_ms'):
        return lines+['상태: 매수 체결 대기 중']
    average=t.get('buy_average') or (t['entry_cost']/t['entry_qty'] if t.get('entry_qty') else 0)
    lines.append('매수가 '+unit_price(average,account['quote']))
    bid,stamp=t.get('last_bid'),t.get('mark_ms');fx=account.get('fx_krw_per_quote')
    if bid and average and fx and stamp is not None and 0<=now_ms-stamp<=15000:
        value=t['remaining_qty']*bid*(1-t['fee_bps']/10000)*(1-t['slippage_bps']/10000)
        pnl=value-t['remaining_cost'];pct=pnl/t['remaining_cost']*100 if t['remaining_cost'] else 0
        lines += ['현재가 '+unit_price(bid,account['quote']),f'현재 순손익 {money(pnl*fx,True)} ({pct:+.2f}%)']
    else: lines += ['현재가·현재 순손익 확인 대기 (최신 시세 없음)']
    if t['status']=='EXIT_PENDING':lines.append('상태: 시장가 청산 대기 중')
    return lines


def portfolio_header(ledger, now_ms):
    data=ledger.snapshot(now_ms)
    accounts=data['accounts']
    invested=sum(t['remaining_cost']*(a['fx_krw_per_quote'] or 0) for a in accounts for t in a['active'])
    funded=all(a['funded_ms'] is not None for a in accounts)
    cash=sum(a['cash_krw'] for a in accounts) if funded else None
    equity=sum(a['equity_krw'] for a in accounts) if all(a['equity_krw'] is not None for a in accounts) else None
    lines=['📊 FAST 모의투자 현황',f'조회 {clock(now_ms)} KST · 당일 기준 07:30',
           '최초원금 4,000,000원',f'매수원금 합계 {money(invested)} / 예수금 {money(cash)}']
    if equity is None:lines.append('평가금액·누적 수익률 확인 대기')
    else:lines.append(f'평가 {equity:,.0f}원 / 누적 {(equity/4000000-1)*100:+.2f}% ({equity-4000000:+,.0f}원)')
    state=ledger.control() if hasattr(ledger,'control') else {'enabled':True}
    lines.append('포착·매매: '+('진행 중' if state['enabled'] else '정지'))
    return lines,accounts


def positions_page(ledger, now_ms, offset=0):
    # One ledger lock gives totals and positions the same accounting instant.
    with ledger.lock:
        lines,accounts=portfolio_header(ledger,now_ms)
        trades=sorted([t for a in accounts for t in a['active']],key=lambda t:(t['signal_ms'],t['id']),reverse=True)
        by_venue={a['venue']:a for a in accounts};size=10
        offset=min(max(0,offset)//size*size,max(0,(len(trades)-1)//size*size))
        lines += [f'보유·매수대기 {len(trades)}건 · {offset//size+1}/{max(1,(len(trades)+size-1)//size)}페이지','']
        for t in trades[offset:offset+size]:lines+=position_lines(t,by_venue[t['venue']],now_ms)+['']
        if not trades:lines.append('현재 보유·매수대기 종목이 없습니다.')
    lines += ['','거래소별 최초 100만원 · 최대 5슬롯 · 슬롯당 최대 20만원.',
              '평가·순손익은 매수 비용과 예상 매도 수수료·슬리피지를 반영한 호가 기준.',
              '해외 금액은 최초 고정환율로 원화 환산 · 모의투자.']
    markup=keyboard();nav=[]
    if offset:nav.append({'text':'◀ 이전','callback_data':f'fast_results:{offset-size}'})
    nav.append({'text':'새로고침','callback_data':f'fast_results:{offset}'})
    if offset+size<len(trades):nav.append({'text':'다음 ▶','callback_data':f'fast_results:{offset+size}'})
    markup['inline_keyboard'].insert(0,nav)
    return '\n'.join(lines),markup


def keyboard():
    return {'inline_keyboard':[
        [{'text':'포착 리스트','callback_data':'nav:fast_captures'},
         {'text':'모의투자 결과','callback_data':'nav:fast_report'}],
        [{'text':'일괄정리 및 포착정지','callback_data':'nav:fast_clear'}],
        [{'text':'포착 및 매매 시작','callback_data':'nav:fast_start'}]]}


def menu(ledger):
    state=ledger.control() if ledger else None
    status='준비 중' if state is None else '진행 중' if state['enabled'] else '정지'
    return ('FAST 모의투자\n상태: '+status+'\n'
            '거래소별 최초 100만원 · 최대 5슬롯 · 슬롯당 최대 20만원\n'
            '당일 기준: 매일 07:30 KST · 자산과 누적 손익은 이어집니다.\n'
            '+12% 익절 · −6% 손절 · 매매 기능은 재확인 후 실행합니다.',keyboard())


def summary(data, daily=False):
    title = '📅 FAST 일일 모의투자 결과' if daily else '📊 FAST 모의투자'
    rows = [title, f'집계 {data["date"]} KST · '+('전일 청산 실현손익' if daily else '오늘 청산 실현손익'),
            '각 거래소 최초 100만원 · 종목당 20만원 · 최대 5종목',
            '시장가 모의매수 · +12% 지정가 익절 · −6% 시장가 손절 · 시간제한 없음',
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
        rows.append(f'현재 보유/대기 {len(a["active"])} · 매도 당일 재매수 금지(07:30 기준)')
        for version,c in a.get('cohorts',{}).items():
            label=('v5 +12%/−6%' if version=='fast-target-v5' else 'v4 현재가 반복' if version=='fast-current-cycle-v4' else 'v3 1틱 반복' if version=='fast-tick-cycle-v3' else
                   'v2 거래량·가속/5분 보유' if version=='fast-volume-accel-v2' else 'v1 매수세/5분 보유')
            rows.append(f'{label} · 완료 {c["closed"]}건 전체 순손익 {money(c["closed_trade_pnl_krw"],True)}')
            if version in ('fast-tick-cycle-v3','fast-current-cycle-v4','fast-target-v5'):
                rows.append(f'지정가 매도 완료 {c["cycles"]}회 · 비용 전 {money(c["gross_krw"],True)} · 수수료 {money(c["fees_krw"])}')
                rows.append(f'추가 슬리피지 {money(c["slippage_krw"])} · 시장가 청산 순손익 {money(c["forced_pnl_krw"],True)} (전체에 포함)')
        if a['unknown_marks']:
            rows.append(f'호가 미확인 {a["unknown_marks"]}건 · 전체 자산평가 보류')
        if a['error']:
            rows.append('시세 조회 오류: '+a['error'][:80])
        if a['heartbeat_ms'] is None or data['now_ms']-a['heartbeat_ms']>30_000:
            rows.append('모의 감시 응답 확인 필요')
        rows.append('')
    rows += ['해외 자금은 최초 공개시세로 환산 후 환산율 고정(환율손익 제외).',
             '메이커/테이커 수수료 가정: 업비트 0.05/0.05%, 빗썸 0.04/0.04%, 바이낸스 0.1/0.1%, 크라켄 0.4/0.8%.',
             '지정가: 대기 물량·실제 체결량 모형. 시장가: 호가 깊이+슬리피지 0.05%.',
             '구버전 비용 유지. 자료 단절·잔량/최소주문 미달은 청산 지연 표시.',
             '모의투자 · 실제 주문 없음 · 매일 07:30 KST 전일 보고']
    return '\n'.join(rows)


def recent(ledger, now_ms):
    if hasattr(ledger, 'result_rows'):
        return positions_page(ledger, now_ms)[0]
    rows = ['📊 FAST 모의검증 결과 · 최근 10건',
            'v5 +12%/−6% · 포착→종료 KST · 순수익률은 배정 20만원 기준', '']
    trades = sessions(ledger,now_ms)
    for t in trades:
        if t.get('execution_version')=='fast-target-v5':
            timing=clock(t['signal_ms'])+'→'+(clock(t['close_ms']) if t.get('close_ms') else '진행')
            result=(f'{return_pct(t):+.2f}% · '+REASONS.get(t['reason'],t['reason'] or '') if t['status']=='CLOSED' else
                    '시장가 청산 대기' if t['status']=='EXIT_PENDING' else '익절/손절 대기' if t.get('entry_ms') else '매수 대기')
            rows.append(f'{NAMES[t["venue"]]} {t["symbol"]} v5 | {timing} | {result}')
            continue
        if tick(t):
            version='v4' if t['execution_version']=='fast-current-cycle-v4' else 'v3'
            timing=clock(t['signal_ms'])+'→'+(clock(t['close_ms']) if t.get('close_ms') else '진행')
            if t['status']=='CLOSED':result=f'{return_pct(t):+.2f}%'
            elif t['status']=='SKIPPED':result='매수 미체결 · 0.00%'
            elif t['status']=='EXIT_PENDING':result='잔량 청산 대기'
            else:result='매수 대기' if not t.get('entry_ms') else '반복 중'
            result+=f' · 왕복 {t["cycles_completed"]}회'
            if t.get('forced_exits'):result+=' · 마지막 시장가'
            if t['status']=='EXIT_PENDING' and now_ms>t['deadline_ms']:result+=' · 10분 초과'
            rows.append(f'{NAMES[t["venue"]]} {t["symbol"]} {version} | {timing} | {result}')
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
        if t.get('execution_version')=='fast-target-v5':
            rows += [f'\n{NAMES[t["venue"]]} {t["symbol"]} v5 · {t["status"]}',
                     f'포착 {clock(t["signal_ms"])} · 종료 '+(clock(t['close_ms']) if t.get('close_ms') else '대기'),
                     f'매수 평균 {t.get("buy_average",0):g} · 익절 주문 {t.get("take_profit_price",0):g} · 손절 기준 {t.get("stop_price",0):g}',
                     f'실현 {money(t["realized_quote"]*fx,True)} ({return_pct(t):+.2f}%/배정금) · '+REASONS.get(t['reason'],t['reason'] or '보유 중')]
            if t.get('last_error'): rows.append('대기 사유: '+t['last_error'])
            continue
        if tick(t):
            version='v4' if t['execution_version']=='fast-current-cycle-v4' else 'v3'
            rows += [f'\n{NAMES[t["venue"]]} {t["symbol"]} {version} · {t["status"]}',
                     f'포착 {clock(t["signal_ms"])} · 기한 {clock(t["deadline_ms"])}',
                     '첫 매수 '+(clock(t['entry_ms']) if t.get('entry_ms') else '미체결')+
                     ' · 마지막 매도 '+(clock(t['last_sell_ms']) if t.get('last_sell_ms') else '없음'),
                     f'왕복 {t["cycles_completed"]}회 · 실현 {money(t["realized_quote"]*fx,True)} ({return_pct(t):+.2f}%/배정금)',
                     f'수수료 {money(t["fees_paid"]*fx)} · 강제청산 {money(t["forced_exit_pnl"]*fx,True)} (포함)']
            if t.get('order'):
                o=t['order']
                if o.get('minimum_sell_price'):
                    rows.append(f'매수 평균 {o["buy_average"]:g} · 최저 매도가 {o["minimum_sell_price"]:g}')
                rows.append(f'{"매수" if o["side"]=="BUY" else "매도"} 지정가 {o["price"]:g} · 미체결 {o["remaining"]:g}')
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
    if kind in ('fast_report','fast_balance'):
        if hasattr(ledger, 'result_rows'):
            return positions_page(ledger,now_ms)
        return recent(ledger,now_ms), keyboard()
    if kind=='fast_orders':
        return history(ledger,now_ms), keyboard()
    date = None
    if kind=='fast_daily':
        from datetime import timedelta
        date = (datetime.fromisoformat(ledger.report_day(now_ms)).date()-timedelta(days=1)).isoformat()
        if date < ledger.report_day(ledger.started_ms):
            return '📅 FAST 전일 결과\n모의투자 시작 전 날짜입니다. 첫 일일 보고는 시작일 다음 날 07:30 KST입니다.', keyboard()
    return summary(ledger.snapshot(now_ms,date),daily=kind=='fast_daily'), keyboard()

