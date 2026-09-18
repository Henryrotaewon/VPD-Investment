import base64
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
from concurrent.futures import ThreadPoolExecutor

# Support both `python magi2/server_runner.py` and package imports.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from magi2.telegram_ui import (COMMANDS, Confirmations, help_text, main_keyboard,
    scan_keyboard, parse_command, magi3_status, validation_text)
from magi2.execution_client import view as execution_view
from magi2.shadow_bridge import start as start_shadow_bridge
from magi2.magi1_intelligence import load_intelligence, fetch_intelligence

ROOT=Path(__file__).resolve().parents[1]
REPO_STATE_DIR=ROOT/'magi2'/'state'
KST=ZoneInfo('Asia/Seoul')
INTERVAL=int(os.getenv('MAGI2_MONITOR_INTERVAL_SEC',os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS','60')))
POLL_SECONDS=5
BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
ALLOWED_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','').strip()
GITHUB_REPO=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
BACKUP_BRANCH=os.getenv('MAGI2_BACKUP_BRANCH','paper-history').strip()
STATE_DIR=REPO_STATE_DIR
BOT_USERNAME=''
CONFIRMATIONS=Confirmations()
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='paper-engine')
ENGINE_JOB=None
ENGINE_MODE=None
ALLOWED_USER_IDS={x.strip() for x in os.getenv('TELEGRAM_ALLOWED_USER_IDS','').split(',') if x.strip()}


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
    global STATE_DIR
    STATE_DIR=volume_dir
    log(f'MAGI2 persistent state active: {volume_dir}'); return volume_dir


def state_signature():
    sig=[]
    for name in ('paper_state.json','paper_events.jsonl'):
        path=STATE_DIR/name
        try: sig.append((name,path.stat().st_mtime_ns,path.stat().st_size))
        except FileNotFoundError: sig.append((name,None,None))
    return tuple(sig)


def github_headers():
    token=os.getenv('GITHUB_TOKEN','').strip()
    if not token: raise RuntimeError('GITHUB_TOKEN 없음')
    return {'Authorization':f'Bearer {token}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}


def ensure_backup_branch():
    headers=github_headers(); api=f'https://api.github.com/repos/{GITHUB_REPO}'
    branch=requests.get(f'{api}/git/ref/heads/{BACKUP_BRANCH}',headers=headers,timeout=20)
    if branch.status_code==200: return
    if branch.status_code!=404: branch.raise_for_status()
    main=requests.get(f'{api}/git/ref/heads/main',headers=headers,timeout=20); main.raise_for_status()
    sha=main.json()['object']['sha']
    created=requests.post(f'{api}/git/refs',headers=headers,json={'ref':f'refs/heads/{BACKUP_BRANCH}','sha':sha},timeout=20)
    if created.status_code not in (201,422): created.raise_for_status()


def github_put_text(path,text,message):
    headers=github_headers()
    api=f'https://api.github.com/repos/{GITHUB_REPO}/contents/{path}'
    current=requests.get(api,headers=headers,params={'ref':BACKUP_BRANCH},timeout=20)
    sha=None
    if current.status_code==200:
        body=current.json(); sha=body.get('sha')
        encoded=(body.get('content') or '').replace('\n','')
        if encoded and base64.b64decode(encoded).decode('utf-8')==text: return False
    elif current.status_code!=404: current.raise_for_status()
    payload={'message':message,'content':base64.b64encode(text.encode('utf-8')).decode('ascii'),'branch':BACKUP_BRANCH}
    if sha: payload['sha']=sha
    response=requests.put(api,headers=headers,json=payload,timeout=30); response.raise_for_status(); return True


def backup_paper_history():
    """Mirror Railway PAPER state and month-partitioned events for analysis."""
    state_path=STATE_DIR/'paper_state.json'; events_path=STATE_DIR/'paper_events.jsonl'
    if not state_path.exists(): return
    ensure_backup_branch()
    changed=github_put_text('magi2/analytics/paper_state_latest.json',state_path.read_text(encoding='utf-8'),'Backup MAGI2 PAPER state')
    months={}
    if events_path.exists():
        for line in events_path.read_text(encoding='utf-8').splitlines():
            if not line.strip(): continue
            try: month=str(json.loads(line).get('ts','unknown'))[:7]
            except Exception: month='unknown'
            if len(month)!=7 or month[4]!='-': month='unknown'
            months.setdefault(month,[]).append(line)
    for month,lines in months.items():
        changed=github_put_text(f'magi2/analytics/paper_events_{month}.jsonl','\n'.join(lines)+'\n',f'Backup MAGI2 PAPER events {month}') or changed
    log(f'GitHub PAPER backup complete; changed={changed}')


def telegram_api(method,payload):
    response=requests.post(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',json=payload,timeout=15)
    response.raise_for_status()
    data=response.json()
    if not data.get('ok'): raise RuntimeError('TELEGRAM_API_REJECTED')
    return data.get('result')


def telegram(text,reply_markup=None):
    if not BOT_TOKEN or not ALLOWED_CHAT_ID: return
    # UTF-16 units remain below Telegram's message size even with emoji.
    chunks=[text[i:i+1800] for i in range(0,len(text),1800)] or [' ']
    try:
        for i,chunk in enumerate(chunks):
            payload={'chat_id':ALLOWED_CHAT_ID,'text':chunk}
            if reply_markup and i==len(chunks)-1: payload['reply_markup']=reply_markup
            telegram_api('sendMessage',payload)
    except Exception as e: log(f'Telegram send error: {type(e).__name__}')


def setup_telegram_menu():
    global BOT_USERNAME
    me=telegram_api('getMe',{})
    BOT_USERNAME=me.get('username','')
    scope={'type':'chat','chat_id':ALLOWED_CHAT_ID}
    commands=[{'command':key,'description':desc} for key,desc in COMMANDS]
    for language in ('','ko'):
        telegram_api('setMyCommands',{'commands':commands,'scope':scope,'language_code':language})
    # Telegram custom menu buttons are private-chat only; slash commands work in groups too.
    if not ALLOWED_CHAT_ID.startswith('-'):
        telegram_api('setChatMenuButton',{'chat_id':ALLOWED_CHAT_ID,'menu_button':{'type':'commands'}})
    log('Telegram command menu registered: Korean v3')


def start_engine(mode):
    global ENGINE_JOB, ENGINE_MODE
    if ENGINE_JOB is not None and not ENGINE_JOB.done():
        return False
    finish_engine_job()
    ENGINE_MODE=mode
    ENGINE_JOB=EXECUTOR.submit(run_engine,mode)
    return True


def finish_engine_job():
    global ENGINE_JOB, ENGINE_MODE
    if ENGINE_JOB is None or not ENGINE_JOB.done(): return
    mode=ENGINE_MODE
    try:
        ENGINE_JOB.result()
        log(f'PAPER job completed: {mode}')
    except Exception as e:
        log(f'PAPER job failed [{mode}]: {type(e).__name__}')
        if mode!='monitor': telegram('작업을 완료하지 못했습니다. /status에서 상태를 확인하세요.')
    ENGINE_JOB=None; ENGINE_MODE=None


def run_engine(mode):
    script='magi2/refill_engine.py' if mode=='refill' else 'magi2/paper_engine.py'
    args=[sys.executable,script] if mode=='refill' else [sys.executable,script,mode]
    before=state_signature(); p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,env=os.environ.copy(),timeout=300)
    if p.stdout: print(p.stdout,end='',flush=True)
    if p.stderr: print(p.stderr,end='',flush=True)
    if p.returncode!=0:
        details=(p.stderr or p.stdout or '').strip()
        if len(details)>1800: details=details[-1800:]
        raise RuntimeError(f'MAGI2 {mode} failed with exit code {p.returncode}\n{details or "(no subprocess output)"}')
    if state_signature()!=before:
        try: backup_paper_history()
        except Exception as e: log(f'GitHub PAPER backup error (engine unaffected): {e}')


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
    title='🌅 오전 VPD 조회' if session=='morning' else '🌙 저녁 VPD 조회'
    lines=[title,f'저장 스캔: {asof}','', '📊 VPD TOP10']
    for i,x in enumerate(top,1):
        dv=x.get('VPDVelocity'); dvtxt='-' if dv is None else f'{float(dv):+.0f}'
        dr=x.get('DistributionRisk','CLEAR'); conf=x.get('VWPIConfidence','N/A')
        lines.append(f"{i}. {x.get('coin','-')} | VPD {num(x.get('VPD'))} | Δ{dvtxt} | {x.get('momentum','-')} | DR {dr} | CONF {conf}")
    lines += ['', '※ 조회 명령: 신규 스캔을 실행하지 않고 직전 자동 스캔본을 반환합니다.']
    telegram('\n'.join(lines))


def signals_text():
    path=os.getenv('MAGI1_INTELLIGENCE_PATH','').strip()
    url=os.getenv('MAGI1_INTELLIGENCE_URL','').strip()
    if not path and not url:
        return '⚡ FAST · Whale 신호\n관측 파일 전달 경로가 아직 연결되지 않았습니다. 신호가 없다는 뜻은 아닙니다.'
    try:
        rows=(load_intelligence(path,int(time.time()*1000)) if path else
              fetch_intelligence(url,os.getenv('MAGI_INTELLIGENCE_TOKEN',''),int(time.time()*1000)))
    except (OSError,ValueError,KeyError,TypeError,requests.RequestException):
        return '⚡ FAST · Whale 신호\n최신 관측을 확인할 수 없습니다. 파일 누락·만료·형식을 점검해야 합니다.'
    lines=['⚡ FAST · Whale 최근 5분 관측 (매매 지시 아님)']
    for row in rows[-15:]:
        venue=row.get('evidence',{}).get('venue','onchain')
        lines.append(f"{row['asset']} | {row['strategy_tag']} | {venue} | {row['direction']} | 점수 {row['heuristic_score']:.1f}")
    if not rows: lines.append('이 관측 구간에 신호가 없습니다.')
    return '\n'.join(lines)


def may_execute(chat_id,user_id):
    if str(chat_id)!=ALLOWED_CHAT_ID: return False
    if ALLOWED_USER_IDS: return str(user_id) in ALLOWED_USER_IDS
    return str(user_id)==str(chat_id) and not str(chat_id).startswith('-')


def handle_command(text,chat_id=None,user_id=None):
    cmd=parse_command(text,BOT_USERNAME)
    chat_id=str(chat_id or ALLOWED_CHAT_ID)
    try:
        if cmd in ('help','menu'):
            telegram(help_text() if cmd=='help' else '📋 메뉴를 선택하세요. 자산보고와 실행 기능은 PAPER 기준입니다.',main_keyboard())
        elif cmd=='scan': telegram('🔎 어떤 VPD 저장본을 조회할까요?',scan_keyboard())
        elif cmd=='morning_scan': return_magi1_state('morning')
        elif cmd=='evening_scan': return_magi1_state('evening')
        elif cmd=='report':
            if not start_engine('report'): telegram('다른 PAPER 작업이 진행 중입니다. 잠시 후 다시 조회하세요.')
        elif cmd in ('morning','refill'):
            if not may_execute(chat_id,user_id):
                telegram('실행 권한이 없는 사용자입니다. 조회 메뉴는 이용할 수 있습니다.'); return
            if ENGINE_JOB is not None and not ENGINE_JOB.done():
                telegram('PAPER 작업이 진행 중입니다. 완료 후 다시 선택하세요.'); return
            label='리밸런싱 (기존 보유 종목의 모의 매도·매수 가능)' if cmd=='morning' else '빈자리 채우기 (모의 신규 매수)'
            telegram(f'PAPER {label}을 실행할까요?\n60초 안에 확인하세요. 실제 주문은 발생하지 않습니다.',
                     CONFIRMATIONS.issue(cmd,chat_id,user_id))
        elif cmd=='cancel':
            CONFIRMATIONS.cancel(chat_id,user_id)
            telegram('대기 중인 실행 확인을 취소했습니다. 이미 진행 중인 작업은 계속됩니다.')
        elif cmd=='status':
            running=ENGINE_MODE if ENGINE_JOB is not None and not ENGINE_JOB.done() else '대기'
            telegram(f'🤖 봇 상태\n텔레그램: 응답 중\n실행 계정: PAPER\nPAPER 작업: {running}\n자동 모니터 간격: {INTERVAL}초\nMAGI1·MAGI3 운영 상태: 이 화면에서는 미조회')
        elif cmd in ('magi3','execution','shadow','orders','assets'): telegram(execution_view(cmd))
        elif cmd=='strategies': telegram(validation_text())
        elif cmd=='signals': telegram(signals_text())
        elif text.strip(): telegram('명령을 찾지 못했습니다. /help 또는 아래 버튼을 이용하세요.',main_keyboard())
    except Exception as e:
        log(f'Command failed [{cmd}]: {type(e).__name__}')
        telegram('명령을 처리하지 못했습니다. 잠시 후 다시 시도하세요.')


def handle_callback(callback):
    msg=callback.get('message') or {}
    chat_id=str((msg.get('chat') or {}).get('id',''))
    user_id=str((callback.get('from') or {}).get('id',''))
    authorized=chat_id==ALLOWED_CHAT_ID
    try:
        telegram_api('answerCallbackQuery',{'callback_query_id':callback['id'],
                     'text':'처리 중' if authorized else '접근할 수 없습니다.'})
    except Exception as e: log(f'Callback acknowledgement failed: {type(e).__name__}')
    if not authorized: return
    data=callback.get('data','')
    if data.startswith('nav:'):
        command=data[4:]
        if command in ('morning_scan','evening_scan','menu','help'):
            handle_command(command,chat_id,user_id)
    elif data.startswith(('confirm:','cancel:')):
        prefix,token=data.split(':',1)
        if not may_execute(chat_id,user_id): return
        action=CONFIRMATIONS.consume(token,chat_id,user_id)
        if action is None:
            telegram('확인이 만료되었거나 이미 처리되었습니다. 메뉴에서 다시 선택하세요.'); return
        try:
            telegram_api('editMessageReplyMarkup',{'chat_id':chat_id,'message_id':msg['message_id'],
                                                   'reply_markup':{'inline_keyboard':[]}})
        except Exception as e: log(f'Keyboard cleanup failed: {type(e).__name__}')
        if prefix=='cancel': telegram('실행을 취소했습니다.'); return
        if start_engine(action): telegram('PAPER 작업을 시작했습니다. 결과는 완료 후 안내합니다.')
        else: telegram('다른 PAPER 작업이 진행 중입니다. 완료 후 다시 선택하세요.')


def get_updates(offset):
    r=requests.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',params={'offset':offset,'timeout':0,'allowed_updates':json.dumps(['message','callback_query'])},timeout=10); r.raise_for_status(); return r.json().get('result',[])


def discard_pending_updates():
    offset=0
    try:
        updates=get_updates(0)
        if updates:
            offset=max(int(x['update_id']) for x in updates)+1; get_updates(offset); log(f'Discarded {len(updates)} pending Telegram update(s) on startup')
    except Exception as e: log(f'Telegram startup flush error: {type(e).__name__}')
    return offset


def poll_updates(offset):
    try:
        for update in get_updates(offset):
            ident=int(update['update_id'])
            if ident<offset: continue
            offset=ident+1
            if 'callback_query' in update:
                handle_callback(update['callback_query']); continue
            msg=update.get('message') or {}
            chat_id=str((msg.get('chat') or {}).get('id',''))
            if chat_id!=ALLOWED_CHAT_ID: continue
            handle_command(msg.get('text') or '',chat_id,str((msg.get('from') or {}).get('id','')))
    except Exception as e: log(f'Telegram polling error: {type(e).__name__}')
    return offset


def main():
    prepare_persistent_state()
    start_shadow_bridge(STATE_DIR,GITHUB_REPO,log)
    if not BOT_TOKEN or not ALLOWED_CHAT_ID: raise RuntimeError('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.')
    try: backup_paper_history()
    except Exception as e: log(f'Initial GitHub PAPER backup error (engine unaffected): {e}')
    try: setup_telegram_menu()
    except Exception as e: log(f'Telegram menu registration failed: {type(e).__name__}')
    if os.getenv('MAGI1_INTELLIGENCE_URL') or os.getenv('MAGI1_INTELLIGENCE_PATH'):
        log('intelligence_probe: '+signals_text().replace('\n',' | ')[:1200])
    offset=discard_pending_updates(); log(f'MAGI Railway authority started; monitor={INTERVAL}s; Telegram console=ON')
    next_monitor=time.monotonic()+INTERVAL
    next_execution_probe=time.monotonic()+30
    while True:
        if time.monotonic()>=next_execution_probe and os.getenv('MAGI3_SERVICE_URL'):
            log('magi3_probe: '+execution_view('execution').replace('\n',' | '))
            next_execution_probe=time.monotonic()+300
        finish_engine_job()
        offset=poll_updates(offset)
        if time.monotonic()>=next_monitor:
            if start_engine('monitor'): next_monitor=time.monotonic()+INTERVAL
        time.sleep(POLL_SECONDS)

if __name__=='__main__': main()
