import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT=Path(__file__).resolve().parents[1]
REPO_STATE_DIR=ROOT/'magi2'/'state'
KST=ZoneInfo('Asia/Seoul')
INTERVAL=int(os.getenv('MAGI2_MONITOR_INTERVAL_SEC',os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS','60')))
POLL_SECONDS=5
BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
ALLOWED_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','').strip()
GITHUB_REPO=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()


def log(msg): print(f'[{datetime.now(KST).isoformat()}] {msg}',flush=True)
def load_json(path): return json.loads(path.read_text(encoding='utf-8'))


def assert_no_recovery_regression(seed_file,live_file):
    seed=load_json(seed_file); live=load_json(live_file); conflicts=[]
    for coin,sp in seed.get('positions',{}).items():
        if sp.get('status')!='CLOSED': continue
        lp=live.get('positions',{}).get(coin)
        if lp and lp.get('entry_at')==sp.get('entry_at') and lp.get('status','OPEN')=='OPEN': conflicts.append(coin)
    if conflicts: raise RuntimeError('RECOVERY CONFLICT: previously CLOSED trade is OPEN: '+', '.join(conflicts))


def prepare_persistent_state():
    configured=os.getenv('MAGI2_STATE_DIR') or os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    volume_dir=Path(configured) if configured else Path('/data/magi2')
    on_railway=bool(os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_PROJECT_ID'))
    if not configured and not volume_dir.exists():
        if on_railway: raise RuntimeError('Railway persistent volume is not mounted. MAGI2 refuses to start.')
        log(f'Persistent volume not mounted; local run uses repository state: {REPO_STATE_DIR}'); return REPO_STATE_DIR
    volume_dir.mkdir(parents=True,exist_ok=True); REPO_STATE_DIR.mkdir(parents=True,exist_ok=True)
    seed_state=REPO_STATE_DIR/'paper_state.json'
    for name in ('paper_state.json','paper_events.jsonl'):
        repo_file=REPO_STATE_DIR/name; volume_file=volume_dir/name
        if not volume_file.exists():
            if repo_file.exists() and not repo_file.is_symlink(): shutil.copy2(repo_file,volume_file); log(f'Bootstrapped {name} -> {volume_file}')
            elif name=='paper_events.jsonl': volume_file.touch()
            else: raise RuntimeError(f'Cannot bootstrap missing MAGI2 state file: {repo_file}')
    if seed_state.exists() and not seed_state.is_symlink(): assert_no_recovery_regression(seed_state,volume_dir/'paper_state.json')
    for name in ('paper_state.json','paper_events.jsonl'):
        repo_file=REPO_STATE_DIR/name; volume_file=volume_dir/name
        if repo_file.is_symlink() or repo_file.exists(): repo_file.unlink()
        repo_file.symlink_to(volume_file)
    log(f'MAGI2 persistent state active: {volume_dir}'); return volume_dir


def telegram(text):
    if not BOT_TOKEN or not ALLOWED_CHAT_ID: return
    try: requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',json={'chat_id':ALLOWED_CHAT_ID,'text':text},timeout=15).raise_for_status()
    except Exception as e: log(f'Telegram send error: {e}')


def run_engine(mode):
    p=subprocess.run([sys.executable,'magi2/paper_engine.py',mode],cwd=ROOT,capture_output=True,text=True,env=os.environ.copy())
    if p.stdout: print(p.stdout,end='',flush=True)
    if p.stderr: print(p.stderr,end='',flush=True)
    if p.returncode!=0: raise RuntimeError(f'MAGI2 {mode} failed with exit code {p.returncode}')


def num(v,d=0):
    try: return f'{float(v):.{d}f}'
    except Exception: return '-'


def return_magi1_state(session):
    """Return the last stored automatic scan. Never starts a new scan."""
    url=f'https://raw.githubusercontent.com/{GITHUB_REPO}/main/data/magi1_upbit_{session}_state.json'
    r=requests.get(url,timeout=20)
    if r.status_code==404:
        telegram(f'ℹ️ MAGI1 {session.upper()} SCAN\n저장된 세션 스캔본이 아직 없습니다.'); return
    r.raise_for_status(); d=r.json(); top=(d.get('top10') or [])[:10]; asof=d.get('asof_kst') or d.get('asof') or '-'
    title='🌅 MAGI1 MORNING SCAN' if session=='morning' else '🌙 MAGI1 EVENING SCAN'
    lines=[title,f'저장 스캔: {asof}','', '📊 VPD TOP10']
    for i,x in enumerate(top,1):
        dv=x.get('VPDVelocity'); dvtxt='-' if dv is None else f'{float(dv):+.0f}'
        dr=x.get('DistributionRisk','CLEAR'); conf=x.get('VWPIConfidence','N/A')
        lines.append(f"{i}. {x.get('coin','-')} | VPD {num(x.get('VPD'))} | Δ{dvtxt} | {x.get('momentum','-')} | DR {dr} | CONF {conf}")
    lines += ['', '※ 조회 명령: 신규 스캔을 실행하지 않고 직전 자동 스캔본을 반환합니다.']
    telegram('\n'.join(lines))


def help_text():
    return ('🤖 MAGI Command Manual\n\nMAGI1 조회\nmagi1 morning scan\nmagi1 evening scan\n※ 자동 스캔: 07:10 / 17:40 KST\n※ 명령은 직전 저장본 조회만 수행\n\nMAGI2\nmagi2 morning\nmagi2 refill\nmagi2 report\nmagi2 help\n\nMAGI2 Monitor는 Railway에서 자동 실행 · PAPER ONLY')


def handle_command(text):
    cmd=' '.join(text.strip().lower().split())
    try:
        if cmd=='magi1 morning scan': return_magi1_state('morning')
        elif cmd=='magi1 evening scan': return_magi1_state('evening')
        elif cmd=='magi2 morning': run_engine('morning')
        elif cmd=='magi2 refill': run_engine('refill')
        elif cmd=='magi2 report': run_engine('report')
        elif cmd=='magi2 help': telegram(help_text())
        elif cmd.startswith('magi1') or cmd.startswith('magi2'): telegram('❓ 알 수 없는 MAGI 명령입니다.\nmagi2 help 로 명령어를 확인하세요.')
    except Exception as e:
        log(f'Command failed [{cmd}]: {e}'); telegram(f'🚨 MAGI 명령 실패\n{cmd}\n{e}')


def get_updates(offset):
    r=requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',params={'offset':offset,'timeout':0,'allowed_updates':json.dumps(['message'])},timeout=10); r.raise_for_status(); return r.json().get('result',[])


def discard_pending_updates():
    offset=0
    try:
        updates=get_updates(0)
        if updates:
            offset=max(int(x['update_id']) for x in updates)+1; get_updates(offset); log(f'Discarded {len(updates)} pending Telegram update(s) on startup')
    except Exception as e: log(f'Telegram startup flush error: {e}')
    return offset


def poll_updates(offset):
    try:
        for update in get_updates(offset):
            offset=max(offset,int(update['update_id'])+1); msg=update.get('message') or {}; chat_id=str((msg.get('chat') or {}).get('id','')); text=msg.get('text') or ''
            if chat_id!=ALLOWED_CHAT_ID: log(f'Ignored Telegram command from unauthorized chat {chat_id}'); continue
            handle_command(text)
    except Exception as e: log(f'Telegram polling error: {e}')
    return offset


def main():
    prepare_persistent_state()
    if not BOT_TOKEN or not ALLOWED_CHAT_ID: raise RuntimeError('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.')
    offset=discard_pending_updates(); log(f'MAGI Railway authority started; monitor={INTERVAL}s; Telegram console=ON')
    next_monitor=time.monotonic()+INTERVAL
    while True:
        offset=poll_updates(offset)
        if time.monotonic()>=next_monitor:
            try: run_engine('monitor')
            except Exception as e: log(f'Monitor error: {e}')
            next_monitor=time.monotonic()+INTERVAL
        time.sleep(POLL_SECONDS)

if __name__=='__main__': main()
