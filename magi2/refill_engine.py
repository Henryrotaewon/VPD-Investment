import csv, io, json, os, subprocess, sys
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

import paper_engine as pe

KST=ZoneInfo('Asia/Seoul')
MIN_VPD=float(os.getenv('MAGI2_REFILL_MIN_VPD','75'))
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
    url=f'https://raw.githubusercontent.com/{repo}/main/data/vpd_all_latest.csv?t={int(pe.now_dt().timestamp())}'
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


def parse_snapshot_asof(snap):
    raw=snap.get('asof') or snap.get('asof_kst')
    if not raw: return None
    try: return datetime.fromisoformat(raw).astimezone(KST)
    except Exception: return None


def fresh_scan_if_possible():
    token=os.getenv('GITHUB_TOKEN','').strip()
    if not token: return False,'Railway GITHUB_TOKEN 없음',None,None
    p=subprocess.run([sys.executable,'scanner/run_vpd_action_v1_6.py'],cwd=pe.ROOT,capture_output=True,text=True,env=os.environ.copy())
    if p.stdout: print(p.stdout,end='',flush=True)
    if p.stderr: print(p.stderr,end='',flush=True)
    if p.returncode!=0:
        detail=(p.stderr or p.stdout or '').strip()[-1200:]
        return False,detail or f'exit {p.returncode}',None,None
    fresh_path=pe.ROOT/'.runtime'/'vpd_latest.json'
    if not fresh_path.exists(): return False,'Fresh Scan 결과 JSON이 생성되지 않았습니다.',None,None
    try: snap=json.loads(fresh_path.read_text(encoding='utf-8'))
    except Exception as e: return False,f'Fresh Scan JSON 읽기 실패: {e}',None,None
    asof=parse_snapshot_asof(snap)
    if asof is None or asof.date()!=pe.now_dt().date(): return False,'Fresh Scan 결과 시각 검증 실패',None,None
    return True,'fresh scan complete',snap,asof


def candidate_rows(snap,active,blocked,repo,allow_remote_rank=True):
    top=[r for r in snap.get('top10',[]) if r.get('coin') not in active and r.get('coin') not in blocked and float(r.get('VPD',0) or 0)>=MIN_VPD]
    if top: return top,'TOP10'
    if allow_remote_rank:
        try:
            rows=[r for r in all_ranked_rows(repo) if r['coin'] not in active and r['coin'] not in blocked and r['VPD']>=MIN_VPD]
            if rows: return rows,'VPD75+ 차순위'
        except Exception as e: print('all-rank fetch error',e)
    return [],None


def fresh_ranked_rows(active,blocked):
    path=pe.ROOT/'.runtime'/'vpd_all_latest.csv'
    if not path.exists(): return []
    rows=[]
    try:
        with path.open('r',encoding='utf-8-sig',newline='') as f:
            for x in csv.DictReader(f):
                try: vpd=float(x.get('VPD',''))
                except Exception: continue
                coin=(x.get('coin') or x.get('Coin') or '').strip()
                market=(x.get('market') or x.get('Market') or (f'KRW-{coin}' if coin else '')).strip()
                if not coin or not market or coin in active or coin in blocked or vpd<MIN_VPD: continue
                try: rank=int(float(x.get('Rank') or x.get('rank') or 999999))
                except Exception: rank=999999
                rows.append(dict(x,coin=coin,market=market,VPD=vpd,Rank=rank))
    except Exception as e:
        print('fresh all-rank read error',e); return []
    rows.sort(key=lambda z:(z['Rank'],-z['VPD']))
    return rows


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

    # No eligible candidate means an unconditional Fresh Scan. The scanner itself
    # sends the normal VPD Search First Telegram message, then MAGI2 reports how
    # the fresh result is used for refill.
    if not rows:
        blocktxt=', '.join(sorted(blocked)) or '-'
        pe.telegram(f'🔄 MAGI2 FRESH SCAN 시작\n기존 VPD {snap.get("asof_kst",asof.isoformat())}\nVPD {MIN_VPD:.0f}+ 리필 후보 없음 → VPD Scanner v1.6 재스캔\n당일 TP/SL 재진입 금지: {blocktxt}\nPAPER ONLY')
        ok,msg,fresh_snap,fresh_asof=fresh_scan_if_possible()
        if not ok:
            pe.telegram(f'⚠️ MAGI2 FRESH SCAN 실패/불가\n{msg}\n기존 포지션 유지 · 현금 유지\nPAPER ONLY'); pe.send_current_status(st); return
        snap,asof=fresh_snap,fresh_asof
        rows,source=candidate_rows(snap,active,blocked,repo,allow_remote_rank=False)
        if not rows:
            extra=fresh_ranked_rows(active,blocked)
            if extra: rows,source=extra,'FRESH VPD75+ 차순위'
        eligible_txt=', '.join(f"{r['coin']}({float(r.get('VPD',0)):.0f})" for r in rows[:10]) or '없음'
        pe.telegram(f'✅ MAGI2 FRESH SCAN 완료\n기준 {snap.get("asof_kst",asof.isoformat())}\n리필 적격 VPD {MIN_VPD:.0f}+: {eligible_txt}\n당일 TP/SL 제외: {blocktxt}\n※ 전체 TOP10/스캔 결과는 직전 VPD Search First 메시지 참조\nPAPER ONLY')

    if not rows:
        blocktxt=', '.join(sorted(blocked)) or '-'
        pe.telegram(f'ℹ️ MAGI2 REFILL\nFresh Scan 후에도 VPD {MIN_VPD:.0f}+ 적격 후보가 없습니다.\n당일 TP/SL 재진입 금지: {blocktxt}\n현금 유지합니다.\nPAPER ONLY'); pe.send_current_status(st); return
    rows=rows[:count]; prices=pe.get_prices([r['market'] for r in rows]); sid=f'{today}-REFILL-{pe.now_dt().strftime("%H%M")}'; bought=[]
    for row in rows:
        if pe.buy_position(st,row,prices.get(row['market']),slot,'REFILL',sid,today): bought.append(row['coin'])
    if not bought: raise RuntimeError('Refill candidates existed, but no PAPER buy could be executed.')
    st['last_refill_at']=pe.now_iso(); st['last_refill_session_id']=sid; st['source_refill_snapshot_asof_kst']=snap.get('asof_kst',asof.isoformat()); pe.save_state(st)
    details='\n'.join(f'{i+1}. {c} | 매수금액 {slot:,.0f}원' for i,c in enumerate(bought))
    blocktxt=', '.join(sorted(blocked)) or '-'
    pe.telegram(f'♻️ MAGI2 REFILL 완료\n{sid}\n후보소스 {source}\nBUY {len(bought)}\n{details}\n당일 TP/SL 재진입 금지: {blocktxt}\n잔여 예수금 {float(st["cash_krw"]):,.0f}원\nPAPER ONLY'); pe.send_current_status(st,'📊 REFILL 후 MAGI2 PAPER 현황')

if __name__=='__main__': refill()
