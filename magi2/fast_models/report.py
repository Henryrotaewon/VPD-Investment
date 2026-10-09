"""Bounded read-only views; querying never creates accounts or triggers orders."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
from .metrics import DAY

NAMES={'derivatives':'FAST-DERIVATIVES','bear':'FAST-BEAR'}
KST=ZoneInfo('Asia/Seoul')


def clock(ts):return datetime.fromtimestamp(ts/1000,KST).strftime('%m/%d %H:%M')

def load(root,key):
    path=Path(root)/'fast-models-v1'/(key+'.sqlite3')
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        db.execute('BEGIN')
        s=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        m=json.loads(db.execute('SELECT payload FROM model_metrics WHERE id=1').fetchone()[0])
        days=[json.loads(r[0]) for r in db.execute('SELECT payload FROM model_days ORDER BY day DESC LIMIT 7')]
        trades=db.execute('SELECT ts,kind,pnl,payload FROM model_trades ORDER BY ts DESC').fetchall()
        return s,m,days,trades
    finally:db.close()


def wins(trades):
    if not trades:return '— (청산 0건)'
    w=sum(t[2]>0 for t in trades);l=sum(t[2]<0 for t in trades)
    return f'{100*w/len(trades):.1f}% ({w}승 {l}패 {len(trades)-w-l}무)'


def summary(root,key,now):
    try:s,m,days,trades=load(root,key)
    except (OSError,sqlite3.Error,ValueError,TypeError):return NAMES[key]+': 원장 준비 중'
    stale=not m['valid'] or not 0<=now-m['last_ms']<=120000
    eq=m['equity'];ret=(eq/m['initial']-1)*100
    gap=' · 관측 공백 포함' if m['gap'] else ''
    return (f"• {NAMES[key]}\n  승률 {wins(trades)} / 수익률 {ret:+.2f}% / MDD {m['mdd']:.2f}%{gap}\n"
            f"  평가 {int(eq):,}원 · 보유 {len(s['positions'])} · 주문대기 {len(s['pending'])}"
            +(' · 최근 관측값' if stale else '')+f"\n  시작 {clock(m['started_ms'])} · 평가 {clock(m['last_ms'])}")


def keyboard():
    return {'inline_keyboard':[
        [{'text':'FAST-DERIVATIVES','callback_data':'nav:fast_derivatives'},{'text':'FAST-BEAR','callback_data':'nav:fast_bear'}],
        [{'text':'📅 일별 비교','callback_data':'nav:fast_model_daily'},{'text':'🔎 데이터 점검','callback_data':'nav:fast_model_data'}],
        [{'text':'새로고침','callback_data':'nav:fast_models'},{'text':'↩️ 전략검증','callback_data':'nav:strategies'}]]}


def view(root,now,section='fast_models'):
    lines=['🧪 FAST 두 모델 비교 [PAPER]', '각 3,000,000원 · 독립 원장 · 실제 주문 없음', '']
    if section in ('fast_models','fast_model_daily'):
        lines.extend(summary(root,k,now) for k in NAMES)
    if section=='fast_model_daily':
        lines+=['','일자: 09:00~다음 날 09:00 KST · 아래 MDD는 당일 기준']
        for k,name in NAMES.items():
            try:s,m,days,trades=load(root,k)
            except (OSError,sqlite3.Error,ValueError,TypeError):continue
            lines.append('\n'+name)
            for d in days:
                complete=d['day']==now//DAY or 0<(d['day']+1)*DAY-d['ts']<=60000
                ret=f"{(d['equity']/d['base']-1)*100:+.2f}%" if d['base'] and d['equity'] is not None and d['valid'] and complete else '자료 부족'
                suffix=' · 부분일' if d['day']==m['started_ms']//DAY else ''
                lines.append(f"{clock(d['day']*DAY)[:5]}{suffix}: {wins([t for t in trades if t[0]//DAY==d['day']])}\n  수익률 {ret} / MDD {d['mdd']:.2f}%"+(' · 관측 공백' if d['gap'] else ''))
    elif section in ('fast_bear','fast_derivatives'):
        k='bear' if section=='fast_bear' else 'derivatives'
        lines.append(summary(root,k,now))
        lines+=['',('완료 5분봉 RSI 회복 → 다음 봉 VWAP·매수세 확인\n손절 최대 1.5% · +2%부터 +1% 보호 · +4% 또는 반등 실패 청산' if k=='bear' else
            '하락 추세 숏 + 현물·선물 가격차 수렴\n현물 4거래소 · 바이낸스/크라켄 선형 무기한 · BTC/ETH/XRP/SOL/NEAR\n6개 가상 지갑 각 50만원 선배치 · 계좌 간 담보 합산 없음')]
        try:
            s,m,days,trades=load(root,k)
            for name,p in list(s['positions'].items())[:10]:
                label=p.get('kind','REBOUND');entry=p.get('entry_ms',0)
                lines.append(f"{p.get('asset',name)} · {label} · {clock(entry)} 진입")
            if k=='derivatives':
                for kind,label in [('SHORT','추세 숏'),('BASIS','현선물 쌍')]:
                    rows=[t for t in trades if t[1]==kind]
                    lines.append(f"{label}: {wins(rows)} · 실현 {int(sum(t[2] for t in rows)):+,}원")
            lines.append('\n최근 완료매매')
            for t,kind,pnl,p in trades[:5]:
                p=json.loads(p);lines.append(f"{clock(t)} {p.get('asset',p.get('symbol',''))} {kind} {int(pnl):+,}원")
        except (OSError,sqlite3.Error,ValueError,TypeError):lines.append('원장 준비 중')
    if section in ('fast_models','fast_model_data'):
        try:
            data=json.loads((Path(root)/'fast-models-v1'/'status.json').read_text())
            lines+=['',f"수집 갱신 {clock(data['ts'])} · 최신 호가 {data['quote_ready']}/{data['quote_total']}",
                    f"BTC 4시간 국면: {data['regime']['state']} · DOWN일 때 반등·숏 진입 검토",
                    f"현선물 비교 조합 {data['derivatives']['pairs']}개"]
            if section=='fast_model_data':
                for v in ('upbit','bithumb','binance','kraken'):
                    feeds=[(k,r) for k,r in data['feeds'].items() if k.startswith(v)]
                    fail=[k for k,r in feeds if r['state']!='OK']
                    lines.append(v+': '+('정상' if feeds and not fail else '일부 수집 대기: '+', '.join(fail)))
                for k,row in sorted(data['pairs'].items(),key=lambda x:-x[1]['net_edge_bps'])[:5]:
                    lines.append(f"{k}\n  가격차 {row['edge_bps']:.1f}bp / 비용후 {row['net_edge_bps']:.1f}bp · 기준표본 {row['samples']}")
                lines+=['업비트·빗썸은 KRW 현물만 사용', 'USD·USDT는 별도 환산 · 수수료는 보수적 모의 가정',
                        '호가·상품정보·환산·펀딩 누락 조합은 진입 보류']
        except (OSError,ValueError,KeyError):lines.append('시세 수집 시작 대기')
    lines+=['','승률=비용 반영 후 전량 청산 · 쌍 거래는 1건',
            '수익률=보유 포함 총자산/최초원금−1 · MDD=평가금액 고점 대비 최대 하락',
            'MDD는 관측값 기준 · 시세 공백 구간은 별도 표시']
    return '\n'.join(lines),keyboard()
