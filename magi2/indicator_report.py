"""Reports use historical marks, never today's balance presented as yesterday's."""
from datetime import datetime,timedelta
from magi2.fast_paper_report import positions_page, keyboard, money
from magi2.fast_session import day


def view(ledger, now_ms, command):
    if ledger is None: return '지표 모의투자 준비 중입니다.',keyboard()
    if command=='fast_daily':
        date=(datetime.fromisoformat(day(now_ms))-timedelta(days=1)).date().isoformat()
        return daily_view(ledger,date,now_ms)
    if command=='fast_orders':return orders_view(ledger,now_ms)
    text,markup=positions_page(ledger,now_ms)
    if command=='fast_balance':
        perf=ledger.performance()
        text += '\n\n누적 분 단위 평가 '+str(perf['valid_samples'])+'회'
        if perf['mdd_pct'] is not None:text += f' · 관측 최대낙폭 {perf["mdd_pct"]:.2f}%'
        text += '\n평가 불가 '+str(perf['missing'])+'회 · 관측 사이의 낙폭은 포함되지 않습니다.'
    return text,markup


def daily_view(ledger, date, now_ms):
    # Only accept actual ISO dates; no dynamic SQL identifiers or paths.
    date=datetime.fromisoformat(date).date().isoformat()
    markup=keyboard()
    current=datetime.fromisoformat(date)
    prev=(current-timedelta(days=1)).date().isoformat()
    nxt=(current+timedelta(days=1)).date().isoformat()
    nav=[dict(text='◀ 전일',callback_data='indicator_daily:'+prev)]
    if nxt<=day(now_ms):nav.append(dict(text='다음 날 ▶',callback_data='indicator_daily:'+nxt))
    markup['inline_keyboard'].insert(0,nav)
    return ledger.daily_text(date,now_ms),markup


def orders_view(ledger, now_ms, offset=0):
    import json
    from magi2.fast_paper_report import outcome_lines, position_lines
    size=6
    with ledger.lock:
        count=ledger.db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]
        offset=min(max(0,offset)//size*size,max(0,(count-1)//size*size))
        rows=ledger.db.execute('SELECT payload FROM paper_trades ORDER BY signal_ms DESC,id DESC LIMIT ? OFFSET ?',
                               (size,offset)).fetchall()
        lines=['📒 FAST 지표 가속도 · 매매 이력',f'전체 {count}건 · {offset//size+1}/{max(1,(count+size-1)//size)}페이지','']
        for payload, in rows:
            t=json.loads(payload);account=ledger.account(t['venue'])
            lines+=(outcome_lines(t,account) if t['status'] in ('CLOSED','SKIPPED') else position_lines(t,account,now_ms))+['']
    markup=keyboard();nav=[]
    if offset:nav.append(dict(text='◀ 이전',callback_data=f'indicator_orders:{offset-size}'))
    nav.append(dict(text='새로고침',callback_data=f'indicator_orders:{offset}'))
    if offset+size<count:nav.append(dict(text='다음 ▶',callback_data=f'indicator_orders:{offset+size}'))
    markup['inline_keyboard'].insert(0,nav)
    return '\n'.join(lines),markup
