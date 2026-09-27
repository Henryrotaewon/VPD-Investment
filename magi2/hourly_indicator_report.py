"""Korean views for the independent hourly PAPER cohort."""
import json
from datetime import datetime,timedelta
from magi2.hourly_indicator import GUIDE,clock,date,DAY
from magi2.fast_paper_report import keyboard
from magi3.accounts import quantity
from magi2.indicator_rebalance import candidates, MIN_CANDIDATES


def menu(ledger):
    from magi2.hourly_indicator import now
    with ledger.lock:
        count=len(candidates(ledger,now()))
    return GUIDE+f'\n\n최신 유효 포착 {count}종목 / 실행기준 {MIN_CANDIDATES}종목 이상\n포착·매매: '+('진행 중' if ledger.control()['enabled'] else '정지'),keyboard()


def positions(ledger,stamp,offset=0):
    with ledger.lock:
        b=ledger.balance(stamp);s=ledger.s;names=s['scan'].get('names',{})
        equity=b['equity'] if b['equity'] is not None else b['last_known_equity']
        lines=['📊 지표가속 모의투자 [PAPER]',f'조회 {clock(stamp)} KST',
               f'총 평가금액 {equity:,.0f}원'+(' · 최근 관측값 포함' if b['unknown'] else ''),
               f'누적 수익률 {(equity/b["initial"]-1)*100:+.2f}% · 총 손익 {equity-b["initial"]:+,.0f}원']
        if b['unknown']:
            lines.append('최신 시세 갱신 대기: '+', '.join(b['unknown']))
            unquoted=[symbol for symbol in b['unknown'] if not s['prices'].get(symbol)]
            if unquoted:lines.append('시세 미확보 · 매수 기준가 임시평가: '+', '.join(unquoted))
        lines += [f'Cohort {s["cohort"]}',f'최초원금 {s["initial"]:,.0f}원',
                  f'매수원금 {b["invested"]:,.0f}원 / 예수금 {b["cash"]:,.0f}원']
        lines += [f'누적 실현손익 {b["realized"]:+,.0f}원',f'보유 {b["positions"]}/{s["slots"]} · 주문대기 {b["pending"]}',
                  '포착·매매: '+('진행 중' if s['enabled'] else '정지'),'일봉 회복 1시간 간격 2회 · 추세청산/초기 보호선',
                  f'교체·리필 기준: 최신 유효 포착 {len(candidates(ledger,stamp))}/10종목','']
        op=s.get('rebalance',{})
        if op:
            phase={'SELLING':'전량 매도 대기','BUYING':'편입 매수 대기','DONE':'처리 완료','CANCELED':'종료 · 미편입금 현금 유지'}.get(op['phase'],op['phase'])
            lines.append(f'최근 수동 실행: {phase} · 매수 {len(op["bought"])} · 미편입 {len(op["canceled"])}')
        for symbol,p in s['positions'].items():
            quote=s['prices'].get(symbol,{})
            px=quote.get('price',p['reference']);fresh=0<=stamp-quote.get('ts',0)<=300000
            ret=(p['qty']*px*(1-s['fee'])*(1-s['slip'])/p['cost']-1)*100
            lines += [f'{names.get(symbol,symbol)} · {symbol}',f'매수 {clock(p["entry_ms"])} · {p["reference"]:g}원 · 원금 {p["cost"]:,.0f}원',
                      f'수량 {quantity(p["qty"])}개 · 현재 {px:g}원 · 순손익 {ret:+.2f}%'+('' if fresh else ' · 최근 관측값(갱신 대기)'),f'초기 보호선 {p["initial_stop"]:g}원','']
        for symbol,o in s['pending'].items():lines.append(f'{symbol} · {"매수" if o["side"]=="BUY" else "매도"} 대기 · {clock(o["earliest"])} 이후 실제 5분봉')
        if not s['positions'] and not s['pending']:lines.append('새 신호 대기 · 현금 보유')
        lines.append('가용현금/빈 슬롯 균등배분 · 매매 편도 수수료·슬리피지 각 0.05%')
    return '\n'.join(lines),keyboard()


def events_view(ledger,stamp,offset=0,kind=None):
    with ledger.lock:
        where="kind='SIGNAL'" if kind else "kind IN ('BUY','SELL','CANCEL')"
        count=ledger.db.execute('SELECT COUNT(*) FROM events WHERE '+where).fetchone()[0]
        offset=min(max(0,offset)//6*6,max(0,(count-1)//6*6))
        rows=ledger.db.execute('SELECT ts,kind,symbol,payload FROM events WHERE '+where+' ORDER BY id DESC LIMIT 6 OFFSET ?',(offset,)).fetchall()
    lines=['⚡ 지표가속 포착' if kind else '📒 지표가속 매매 이력',f'전체 {count}건 · {offset//6+1}/{max(1,(count+5)//6)}페이지','']
    labels={'ACCEPTED':'매수 접수','HOLDING_OR_PENDING':'보유·주문대기 중','ALL_SLOTS_USED':'10슬롯 사용 중','INSUFFICIENT_CASH':'현금 부족','STALE_AFTER_EXIT':'청산 이전 신호','MANUAL_REBALANCE_BUSY':'교체·리필 진행 중'}
    for ts,k,symbol,payload in rows:
        p=json.loads(payload);lines.append(f'{symbol} · {clock(ts)} · '+{'SIGNAL':'포착','BUY':'매수','SELL':'매도','CANCEL':'주문 취소'}[k])
        if k=='SIGNAL':lines.append(labels.get(p['decision'],p['decision'])+f' · 초기 보호선 {p["low"]:g}원')
        elif k in ('BUY','SELL'):
            lines.append(f'수량 {quantity(p["qty"])}개 · 기준가격 {p["reference"]:g}원 · 체결봉 {clock(p["fill_ms"])}')
            if k=='SELL':lines.append(f'실현손익 {p["pnl"]:+,.0f}원 · '+{'DAILY_WEAKNESS_AND_PREVIOUS_LOW_BREAK':'일봉 추세청산','HOURLY_CLOSE_BELOW_INITIAL_STRUCTURE':'초기 보호선 이탈','MANUAL_CLEAR':'일괄정리','MANUAL_INDICATOR_REBUILD':'수동 전량교체'}.get(p['reason'],p['reason']))
        lines.append('')
    if not rows:lines.append('아직 기록이 없습니다.')
    mark=keyboard();prefix='captures:' if kind else 'indicator_orders:';nav=[]
    if offset:nav.append(dict(text='◀ 이전',callback_data=prefix+str(offset-6)))
    nav.append(dict(text='새로고침',callback_data=prefix+str(offset)))
    if offset+6<count:nav.append(dict(text='다음 ▶',callback_data=prefix+str(offset+6)))
    mark['inline_keyboard'].insert(0,nav);return '\n'.join(lines),mark


def daily_view(ledger,day,stamp):
    day=datetime.fromisoformat(day).date().isoformat()
    with ledger.lock:
        rows=[json.loads(r[0]) for r in ledger.db.execute('SELECT payload FROM marks WHERE date=? ORDER BY ts',(day,))]
        valid=[r for r in rows if r['equity'] is not None]
        start=int(datetime.fromisoformat(day+'T00:00:00+09:00').timestamp()*1000)
        trades=[json.loads(r[0]) for r in ledger.db.execute("SELECT payload FROM events WHERE kind='SELL' AND ts>=? AND ts<?",(start,start+DAY))]
    lines=['📅 지표가속 일별 평가 [PAPER]',day+' 00:00~24:00 KST',f'평가 관측 {len(valid)}/{len(rows)}회',f'당일 실현손익 {sum(t["pnl"] for t in trades):+,.0f}원']
    if valid:
        v=valid[-1];lines += [f'마지막 관측 {clock(v["ts"])}',f'평가 {v["equity"]:,.0f}원 · 누적 {(v["equity"]/v["initial"]-1)*100:+.2f}%']
    else:lines.append('저장된 유효 평가 없음')
    lines.append('당시 저장된 평가값 · 최종 관측이 일말과 다를 수 있음')
    mark=keyboard();d=datetime.fromisoformat(day);nav=[dict(text='◀ 전일',callback_data='indicator_daily:'+(d-timedelta(days=1)).date().isoformat())]
    nxt=(d+timedelta(days=1)).date().isoformat()
    if nxt<=date(stamp):nav.append(dict(text='다음 날 ▶',callback_data='indicator_daily:'+nxt))
    mark['inline_keyboard'].insert(0,nav);return '\n'.join(lines),mark


def view(ledger,stamp,command):
    if command=='fast_daily':return daily_view(ledger,date(stamp),stamp)
    if command=='fast_orders':return events_view(ledger,stamp)
    return positions(ledger,stamp)
