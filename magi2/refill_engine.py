import csv, io, os, subprocess, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

import paper_engine as pe

KST=ZoneInfo('Asia/Seoul')
MIN_VPD=float(os.getenv('MAGI2_REFILL_MIN_VPD','75'))
FRESH_MINUTES=int(os.getenv('MAGI2_REFILL_FRESH_MINUTES','30'))
REASONS={'TAKE_PROFIT','HARD_STOP'}


def same_day_cooldown(st,today):
    blocked=set()
    for coin,p in st.get('positions',{}).items():
        if p.get('status')!='CLOSED' or p.get('exit_reason') not in REASONS: continue
        raw=p.get('exit_at')
        if not raw: continue
        try:
            if datetime.fromisoformat(raw).astimezone(KST).date().isoformat()==today: blocked.add(coin)
        except Exception: pass
    for p in st.get('closed_positions',[]):
        coin=p.get('coin'); raw=p.get('exit_at')
        if not coin or p.get('exit_reason') not in REASONS or not raw: continue
        try:
            if datetime.fromisoformat(raw).astimezone(KST).date().isoformat()==today: blocked.add(coin)
        except Exception: pass
    return blocked


def all_ranked_rows(repo):
    url=f'https://raw.githubusercontent.com/{repo}/main/data/vpd_all_latest.csv'
    r=requests.get(url,timeout=20); r.raise_for_status()
    rows=[]
    for x in csv.DictReader(io.StringIO(r.text)):
        try: vpd=float(x.get('VPD',''))
        except Exception: continue
        coin=(x.get('coin') or x.get('Coin') or '').strip()
        market=(x.get('market') or x.get('Market') or (f'KRW-{coin}' if coin else '')).strip()
        if not coin or not market: continue
        try: rank=int(float(x.get('Rank') or x.get('rank') or 999999))
        except Exception: rank=999999
        rows.append(dict(x,coin=coin,market=market,VPD=vpd,Rank=rank))
    rows.sort(key=lambda z:(z['Rank'],-z['VPD']))
    return rows


def fresh_scan_if_possible():
    token=os.getenv('GITHUB_TOKEN','').strip()
    if not token: return False,'GITHUB_TOKEN 없음'
    p=subprocess.run([sys.executable,'scanner/run_vpd_action_v1_6.py'],cwd=pe.ROOT,capture_output=True,text=True,env=os.environ.copy())
    if p.returncode!=0:
        detail=(p.stderr or p.stdout or '').strip()[-800:]
        return False,detail or f'exit {p.returncode}'
    return True,'fresh scan complete'


def candidate_rows(snap,active,blocked,repo):
    top=[r for r in snap.get('top10',[]) if r.get('coin') not in active and r.get('coin') not in blocked and float(r.get('VPD',0) or 0)>=MIN_VPD]
    if top: return top,'TOP10'
    try:
        rows=[r for r in all_ranked_rows(repo) if r['coin'] not in active and r['coin'] not in blocked and r['VPD']>=MIN_VPD]
        if rows: return rows,'VPD75+ 차순위'
    except Exception as e: print('all-rank fetch error',e)
    return [],None


def refill():
    st=pe.load_state(); pe.migrate_state(st)
    loaded=pe.load_today_snapshot()
    if not loaded: raise RuntimeError("Today's VPD snapshot is unavailable. Refill aborted.")
    snap,asof=loaded; now=pe.now_dt(); today=now.date().isoformat(); slot=pe.derive_daily_equal_buy(st,today)
    if not slot: raise RuntimeError("Today's daily_equal_buy_krw is unavailable.")
    top_n=int(pe.CFG.get('session',{}).get('top_n',10)); active={c:p for c,p in st.get('positions',{}).items() if p.get('status')=='OPEN'}
    vacant=max(0,top_n-len(active)); cash=float(st.get('cash_krw',0)); count=min(vacant,int((cash+1e-9)//slot))
    if count<=0:
        pe.telegram(f'ℹ️ MAGI2 REFILL\n빈자리 {vacant} / 예수금 {cash:,.0f}원 / 당일 균등매수원가 {slot:,.0f}원\n리필 가능한 슬롯이 없습니다.\nPAPER ONLY'); pe.send_current_status(st); return
    blocked=same_day_cooldown(st,today); repo=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
    rows,source=candidate_rows(snap,active,blocked,repo)
    age=(now-asof).total_seconds()/60
    if (not rows) and age>=FRESH_MINUTES:
        ok,msg=fresh_scan_if_possible()
        if ok:
            loaded=pe.load_today_snapshot()
            if loaded:
                snap,asof=loaded; rows,source=candidate_rows(snap,active,blocked,repo)
        else: print('fresh scan skipped/failed:',msg)
    if not rows:
        blocktxt=', '.join(sorted(blocked)) or '-'
        pe.telegram(f'ℹ️ MAGI2 REFILL\nVPD {MIN_VPD:.0f}+ 적격 후보가 없습니다.\n당일 TP/SL 재진입 금지: {blocktxt}\n현금 유지합니다.\nPAPER ONLY'); pe.send_current_status(st); return
    rows=rows[:count]; prices=pe.get_prices([r['market'] for r in rows]); sid=f'{today}-REFILL-{now.strftime("%H%M")}'; bought=[]
    for row in rows:
        if pe.buy_position(st,row,prices.get(row['market']),slot,'REFILL',sid,today): bought.append(row['coin'])
    if not bought: raise RuntimeError('Refill candidates existed, but no PAPER buy could be executed.')
    st['last_refill_at']=pe.now_iso(); st['last_refill_session_id']=sid; st['source_refill_snapshot_asof_kst']=snap.get('asof_kst',asof.isoformat()); pe.save_state(st)
    details='\n'.join(f'{i+1}. {c} | 매수금액 {slot:,.0f}원' for i,c in enumerate(bought))
    blocktxt=', '.join(sorted(blocked)) or '-'
    pe.telegram(f'♻️ MAGI2 REFILL 완료\n{sid}\n후보소스 {source}\nBUY {len(bought)}\n{details}\n당일 TP/SL 재진입 금지: {blocktxt}\n잔여 예수금 {float(st["cash_krw"]):,.0f}원\nPAPER ONLY'); pe.send_current_status(st,'📊 REFILL 후 MAGI2 PAPER 현황')

if __name__=='__main__': refill()
