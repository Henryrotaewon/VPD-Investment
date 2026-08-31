"""Deterministic MAGI1 Telegram briefing from the stored Upbit scan state.
No new market scan is performed here. VPD values are read exactly from the session state.
"""
import argparse, json, os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo('Asia/Seoul')


def f(v, default=0.0):
    try: return float(v)
    except Exception: return default


def parse_asof(raw):
    if not raw: return None
    s = str(raw).replace(' KST','').strip()
    try: return datetime.fromisoformat(s).replace(tzinfo=KST) if '+' not in s else datetime.fromisoformat(s).astimezone(KST)
    except Exception: return None


def telegram(text):
    token=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); chat=os.getenv('TELEGRAM_CHAT_ID','').strip()
    if not token or not chat: raise RuntimeError('Telegram secrets are missing')
    r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':text},timeout=20)
    r.raise_for_status()


def risk_label(x):
    dr=x.get('DistributionRisk','CLEAR')
    if dr=='HIGH': return '분배위험 HIGH'
    if dr=='WATCH': return '분배주의'
    one=f(x.get('Change1D%') if x.get('Change1D%') is not None else x.get('1D%'))
    if one >= 15: return '가격과열'
    if x.get('Rocket'): return 'Rocket'
    return '관찰'


def freshness_error(session, dt, asof):
    """Accept a recent manual test state, while protecting scheduled briefings from stale data.

    Scheduled production pairing is morning scan 07:10 -> brief 07:30 and
    evening scan 17:40 -> brief 18:00 KST. Manual workflow_dispatch tests can
    happen at any clock hour, so a recent state (<= 90 min old) is valid.
    """
    now = datetime.now(KST)
    if not dt:
        return f'🚨 MAGI1 {session.upper()} 브리핑 보류\n스캔 시각을 해석할 수 없습니다.\n마지막 스캔: {asof}'
    age = now - dt
    if timedelta(0) <= age <= timedelta(minutes=90):
        return None
    return f'🚨 MAGI1 {session.upper()} 브리핑 보류\n저장된 스캔본이 최신 데이터가 아닙니다.\n마지막 스캔: {asof}'


def build(session, d):
    top=(d.get('top10') or [])[:10]; asof=d.get('asof_kst') or d.get('asof') or '-'
    dt=parse_asof(asof)
    err=freshness_error(session, dt, asof)
    if err: return err
    if not top: return f'🚨 MAGI1 {session.upper()} 브리핑 보류\nTOP10 데이터가 없습니다.\n{asof}'

    title='🌅 MAGI1 아침 브리핑' if session=='morning' else '🌙 MAGI1 저녁 브리핑'
    triggers=d.get('triggers') or []; rockets=d.get('qualified_rockets') or []
    a=sum(1 for x in triggers if x.get('trigger')=='A'); b=sum(1 for x in triggers if x.get('trigger')=='B')
    high=sum(1 for x in top if x.get('DistributionRisk')=='HIGH')
    watch=sum(1 for x in top if x.get('DistributionRisk')=='WATCH')
    lines=[title,f'기준 {asof}',f'Trigger A {a} | B {b} | Qualified Rocket {len(rockets)} | DR HIGH {high} / WATCH {watch}','', '📊 VPD TOP10']
    for i,x in enumerate(top,1):
        v=f(x.get('VPD')); dv=x.get('VPDVelocity'); mom=x.get('momentum','-')
        dvtxt='-'
        if dv is not None: dvtxt=f'{f(dv):+.0f}'
        one=x.get('Change1D%')
        if one is None: one=x.get('1D%')
        if one is None: one=x.get('PriceChange1D%')
        lines.append(f"{i}. {x.get('coin','-')} | {v:.0f} | Δ{dvtxt} | {mom} | 1D {f(one):+.2f}% | {risk_label(x)}")

    focus=[]
    def add(x):
        if x and x.get('coin') not in [y.get('coin') for y in focus]: focus.append(x)
    add(max(top,key=lambda x:f(x.get('VPD'))))
    add(max(top,key=lambda x:f(x.get('VPDVelocity'))))
    risky=[x for x in top if x.get('DistributionRisk')=='HIGH'] or [x for x in top if x.get('Rocket')]
    if risky: add(max(risky,key=lambda x:f(x.get('VPD'))))
    for x in top:
        if len(focus)>=3: break
        add(x)
    lines += ['', '🎯 핵심 관찰']
    for x in focus[:3]:
        lines.append(f"• {x.get('coin')} — VPD {f(x.get('VPD')):.0f}, Δ{f(x.get('VPDVelocity')):+.0f}, {x.get('momentum','-')}, DR {x.get('DistributionRisk','CLEAR')}, VWPI CONF {x.get('VWPIConfidence','N/A')}")

    if session=='morning':
        lines += ['', '🧭 대응: 09:00 업비트 일봉 전환 전까지 TOP10 수급 지속 여부를 우선 확인. VPD 고점수만으로 추격매수하지 않음.']
    else:
        lines += ['', '🧭 대응: 아침 대비 VPD 가속·신규 TOP10과 Distribution Risk를 함께 확인. 저녁 스캔 자체는 MAGI2 리밸런싱/리필을 실행하지 않음.']
    lines += ['', 'Source of Truth: UPBIT_KRW_OHLCV | MAGI1 v1.6 | PAPER/판단용']
    return '\n'.join(lines)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('session',choices=['morning','evening']); args=ap.parse_args()
    path=ROOT/'data'/f'magi1_upbit_{args.session}_state.json'
    if not path.exists(): raise RuntimeError(f'MAGI1 {args.session} scan state is missing: {path}')
    d=json.loads(path.read_text(encoding='utf-8')); telegram(build(args.session,d))

if __name__=='__main__': main()
