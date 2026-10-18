"""Read-only venue scope, causal readiness and independent PAPER results."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo

NAMES={'upbit':'업비트','bithumb':'빗썸','binance':'바이낸스','kraken':'크라켄'}


def path(root, venue):
    base=Path(root)/'fast-observe'
    return base if venue=='upbit' else base/'venues'/venue


def clock(t):
    return datetime.fromtimestamp(t/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S') if t else '대기'


def status(root, venue, now=None):
    now=now or int(time.time()*1000)
    try:data=json.loads((path(root,venue)/'status.json').read_text())
    except (OSError,ValueError):return None,{},'시작 대기'
    observation=data.get('observation') or {}
    age=now-data.get('observation_ms',0)
    state=('관측 중' if 0<=age<=120000 and observation.get('connected_groups',0)>0 else
           '관측 갱신 지연' if observation else data.get('detail','시작 중'))
    return data,observation,state


def summary_lines(root, now=None):
    lines=['거래소별 FAST 감시 범위']
    for venue,name in NAMES.items():
        _,o,state=status(root,venue,now)
        u=o.get('universe') or {}
        lines.append(f"{name}: {state} · 등록 {u.get('subscribed',0)}/{u.get('listed',0)} · 기준자료 대기 {o.get('blocked_symbols',0)}")
    return lines


def result_lines(root, now=None):
    """Append accounts without merging different quote currencies or periods."""
    lines=[]
    now=now or int(time.time()*1000)
    for venue in ('bithumb','binance','kraken'):
        data,o,state=status(root,venue,now)
        if data is None:continue
        p=o.get('paper') or {};policy=o.get('paper_policy') or {};fx=o.get('fx') or {}
        if not p:
            lines += [f"• FAST · {NAMES[venue]}: {state} · 모의계좌 준비 중"]
            continue
        current_fx=fx.get('bid') if 0<=now-fx.get('ts',0)<=180000 else None
        equity=p['equity']*current_fx if current_fx else None
        wins=f"{p['winning']/p['closed']*100:.1f}% ({p['winning']}/{p['closed']})" if p['closed'] else '— (청산 0건)'
        money=f'{equity:,.0f}원' if equity is not None else '환율 갱신 대기'
        krw_return=(equity/policy.get('initial_krw',3000000)-1)*100 if equity is not None else None
        ret=f'{krw_return:+.2f}%' if krw_return is not None else '확인 대기'
        stale=' · 최근 관측값' if now-data.get('observation_ms',0)>120000 or p.get('stale_marks',0) else ''
        lines += [f'• FAST · {NAMES[venue]}',f'투자원금 3,000,000원 · 평가 {money}{stale}',
                  f'원화 수익률 {ret} · 승률 {wins}']
    return lines


def view(root):
    now=int(time.time()*1000)
    lines=['FAST 거래소별 운용 [PAPER]','조회 '+clock(now)+' KST','',*summary_lines(root,now),'']
    lines += result_lines(root,now)
    for venue in ('bithumb','binance','kraken'):
        data,o,state=status(root,venue,now)
        if not data:continue
        lines += ['',NAMES[venue]+' · '+state,
                  f"체결 수신 {o.get('trade_received',0)}종목 · 호가 수신 {o.get('book_received',0)}종목",
                  f"모의보유 {(o.get('paper') or {}).get('positions',0)} · 매수대기 {(o.get('paper') or {}).get('pending',0)}",
                  o.get('history_note','기준자료 준비 중')]
        for symbol,f in (o.get('focus') or {}).items():
            ref=f.get('reference') or {}
            lines.append(f"{symbol} 체결 {clock(f.get('trade_ms'))} · 호가 {clock(f.get('book_ms'))} · 비교 {ref.get('baseline_days',0)}/10일")
        try:
            with sqlite3.connect((path(root,venue)/'paper-v1.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
                row=db.execute('SELECT payload FROM state WHERE id=1').fetchone()
                account=json.loads(row[0]) if row else {}
                for symbol,p in list(account.get('positions',{}).items())[:5]:
                    ret=(p['qty']*p['mark']/p['cost']-1)*100
                    lines.append(f"보유 {symbol} · {ret:+.2f}% · 매수 {clock(p.get('entry_ms',p.get('ts')))}")
                rows=db.execute('SELECT ts,symbol,side,pnl FROM fills ORDER BY id DESC LIMIT 3').fetchall()
                for ts,symbol,side,pnl in rows:
                    lines.append(f"{clock(ts)} {symbol} {'매수' if side=='BUY' else '매도'} · 실현 {pnl:+.4g} {o.get('currency','')}")
        except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
            lines.append('매매 원장 확인 대기')
    lines += ['','각 거래소 독립 가상원금 300만원 · 최대 10종목',
              '해외는 시작 시 현지 통화로 가상 환전 · 현재 공개 매수호가 환율로 원화 평가',
              '비교자료 10일 미완비 종목은 포착 대기 · 과거 급등 소급 매수 없음',
              '가정 수수료 빗썸 0.25%·바이낸스 0.10%·크라켄 0.40%, 슬리피지 편도 0.05%']
    return '\n'.join(lines),{'inline_keyboard':[[{'text':'↩️ FAST 전략세부','callback_data':'paper:status:fast:0'}]]}
