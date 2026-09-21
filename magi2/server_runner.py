import base64
import json
import os
import re
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
    scan_keyboard, status_keyboard, vpd_keyboard, shadow_keyboard, role_text, role_keyboard, BOT_NAME, BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION, parse_command, magi3_status, validation_text, observation_text)
from magi2.strategy_guide import strategy_keyboard, strategy_text
from magi2.execution_client import view as execution_view
from magi2.shadow_bridge import start as start_shadow_bridge
from magi2.magi1_intelligence import load_intelligence, fetch_intelligence
from magi2.wave_view import WaveClient, render as render_wave
from magi2.point_in_time_scan import build_snapshot as build_vpd, read_snapshot as read_vpd, cutoff_for
from magi2.market_regime import view as regime_view, keyboard as regime_keyboard, unavailable as regime_unavailable

ROOT=Path(__file__).resolve().parents[1]
REPO_STATE_DIR=ROOT/'magi2'/'state'
KST=ZoneInfo('Asia/Seoul')
INTERVAL=int(os.getenv('MAGI2_MONITOR_INTERVAL_SEC',os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS','60')))
LONG_POLL_SECONDS=25
TELEGRAM_HTTP=requests.Session()
PROBE_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='status-probe')
BOT_TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip()
ALLOWED_CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','').strip()
GITHUB_REPO=os.getenv('MAGI_GITHUB_REPO','Henryrotaewon/VPD-Investment').strip()
BACKUP_BRANCH=os.getenv('MAGI2_BACKUP_BRANCH','paper-history').strip()
STATE_DIR=REPO_STATE_DIR
BOT_USERNAME=''
CONFIRMATIONS=Confirmations()
EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='paper-engine')
SCAN_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='vpd-request-scan')
REGIME_EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix='market-regime-view')
REGIME_JOB=None
SCAN_JOB=None
SCAN_CONTEXT=None
ENGINE_JOB=None
ENGINE_MODE=None
ENGINE_REQUEST_ID=None
FAST_MONITOR=None
FAST_PAPER=None
WAVE_CLIENT=None
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
    response=TELEGRAM_HTTP.post(f'https://api.telegram.org/bot{BOT_TOKEN}/{method}',json=payload,timeout=15)
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
    # Profile updates are independent of command registration; never log tokens.
    for language in ('','ko'):
        for method, field, value in [('setMyName','name',BOT_NAME),
                                     ('setMyDescription','description',BOT_DESCRIPTION),
                                     ('setMyShortDescription','short_description',BOT_SHORT_DESCRIPTION)]:
            try:
                telegram_api(method,{field:value,'language_code':language})
                log(f'MAGI profile updated: {method} language={language or "default"}')
            except Exception as exc:
                log(f'MAGI profile update failed: {method} {type(exc).__name__}')
    scope={'type':'chat','chat_id':ALLOWED_CHAT_ID}
    commands=[{'command':key,'description':desc} for key,desc in COMMANDS]
    for language in ('','ko'):
        telegram_api('setMyCommands',{'commands':commands,'scope':scope,'language_code':language})
    # Telegram custom menu buttons are private-chat only; slash commands work in groups too.
    if not ALLOWED_CHAT_ID.startswith('-'):
        telegram_api('setChatMenuButton',{'chat_id':ALLOWED_CHAT_ID,'menu_button':{'type':'commands'}})
    log('MAGI Telegram menu registered: Korean v16; FAST tick-cycle paper available')


def refresh_telegram_keyboard():
    """Replace a client's persistent legacy keyboard once per menu/chat version."""
    marker=STATE_DIR/'telegram_keyboard.json'
    expected={'version':'magi-menu-v16','chat_id':ALLOWED_CHAT_ID,'bot_username':BOT_USERNAME}
    try:
        if load_json(marker)==expected: return
    except (OSError,ValueError): pass
    telegram_api('sendMessage',{'chat_id':ALLOWED_CHAT_ID,
        'text':'⚡ FAST 모의매매를 1틱 반복 방식으로 적용했습니다.\n'
               '현재가−1틱 매수 → 체결 후 현재가+1틱 매도를 포착 후 10분 동안 반복합니다.\n'
               '10분 종료 시 잔량은 시장가 청산 · 기존 이력 유지 · 실제 주문 없음\n'
               '📊 FAST 모의검증 결과에서 v3 결과를 확인하세요.',
        'reply_markup':main_keyboard()})
    marker.parent.mkdir(parents=True,exist_ok=True)
    temporary=marker.with_suffix('.tmp')
    temporary.write_text(json.dumps(expected),encoding='utf-8'); temporary.replace(marker)
    log('MAGI reply keyboard refreshed: magi-menu-v16')


def start_regime_job():
    global REGIME_JOB
    if REGIME_JOB is not None:
        telegram('시장 국면을 조회 중입니다. 결과를 곧 보내드리겠습니다.')
        return
    # Separate worker: public-data waits never occupy PAPER or FAST workers.
    REGIME_JOB=REGIME_EXECUTOR.submit(regime_view)
    log('market_regime requested: read_only=true')
    telegram('🧭 최신 일봉으로 시장 국면을 조회하고 있습니다.')


def finish_regime_job():
    global REGIME_JOB
    if REGIME_JOB is None or not REGIME_JOB.done(): return
    job=REGIME_JOB
    REGIME_JOB=None
    try: message=job.result()
    except Exception as exc:
        log(f'market_regime failed: {type(exc).__name__}')
        message=regime_unavailable()
    telegram(message,regime_keyboard())
    log('market_regime completed: read_only=true')


def start_engine(mode,request_id=None):
    global ENGINE_JOB, ENGINE_MODE, SCAN_JOB, SCAN_CONTEXT
    if ENGINE_JOB is None or ENGINE_JOB.done(): finish_engine_job()
    if mode in ('morning','rebuild'):
        if SCAN_JOB is not None or (ENGINE_JOB is not None and ENGINE_MODE in ('morning','rebuild')): return False
        now=datetime.now(KST)
        SCAN_CONTEXT={'asof':cutoff_for(now).isoformat(),'request_id':request_id,'mode':mode}
        SCAN_JOB=SCAN_EXECUTOR.submit(build_vpd,STATE_DIR/'vpd_rebalance.json',now)
        log(f'VPD scan started: mode={mode} asof={SCAN_CONTEXT["asof"]} request={request_id or "telegram"}')
        action='전량 교체' if mode=='rebuild' else '리밸런싱'
        telegram(f'🔎 VPD {action} 자료를 새로 조회하고 있습니다.\n'
                 f'기준: {cutoff_for(now):%m/%d %H:%M} KST까지 완료된 분봉\n'
                 f'전 종목을 같은 시각 기준으로 분석한 뒤 요청한 모의 {action} 작업을 실행합니다. 수 분 걸릴 수 있으며 기존 보유분 모니터는 계속 작동합니다.')
        return True
    if ENGINE_JOB is not None and not ENGINE_JOB.done(): return False
    if mode=='refill' and SCAN_JOB is not None: return False
    ENGINE_MODE=mode
    ENGINE_JOB=EXECUTOR.submit(run_engine,mode)
    return True


def record_request(request_id,status,**fields):
    if not request_id: return
    path=STATE_DIR/'rebalance_requests.json'
    data=load_json(path) if path.exists() else {}
    data[request_id]=dict(data.get(request_id,{}),status=status,updated_at=datetime.now(KST).isoformat(),**fields)
    # Keep consumed IDs: a restart must never re-run an old deployment request.
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8'); temporary.replace(path)
    log(f'VPD one-shot request={request_id} status={status}')


def finish_scan_job():
    global SCAN_JOB, SCAN_CONTEXT, ENGINE_JOB, ENGINE_MODE, ENGINE_REQUEST_ID
    if SCAN_JOB is None or not SCAN_JOB.done(): return
    try:
        snapshot=SCAN_JOB.result()
    except Exception as e:
        log(f'VPD scan failed: {type(e).__name__}')
        record_request(SCAN_CONTEXT.get('request_id'),'scan_failed',error=type(e).__name__)
        SCAN_JOB=None; SCAN_CONTEXT=None
        telegram('VPD 자료를 완성하지 못해 리밸런싱을 보류했습니다.\n잠시 후 리밸런싱을 다시 요청하세요. 이번 요청으로 매매하지 않았습니다.')
        return
    if ENGINE_JOB is not None and not ENGINE_JOB.done(): return
    finish_engine_job()
    context=SCAN_CONTEXT; SCAN_JOB=None; SCAN_CONTEXT=None
    if (snapshot.get('asof')!=context['asof'] or
            read_vpd(STATE_DIR/'vpd_rebalance.json',datetime.now(KST),context['asof']) is None):
        record_request(context.get('request_id'),'scan_expired')
        telegram('스캔 기준 시각이 오래됐거나 달라져 리밸런싱을 보류했습니다. 다시 요청하면 새 자료를 생성합니다.'); return
    log(f'VPD scan ready: asof={snapshot["asof"]} markets={snapshot.get("market_count")} analysed={snapshot.get("analysed_count")}')
    ENGINE_MODE=context['mode']; ENGINE_REQUEST_ID=context.get('request_id')
    record_request(ENGINE_REQUEST_ID,'executing',asof=snapshot['asof'])
    ENGINE_JOB=EXECUTOR.submit(run_engine,ENGINE_MODE,snapshot['asof'])


def finish_engine_job():
    global ENGINE_JOB, ENGINE_MODE, ENGINE_REQUEST_ID
    if ENGINE_JOB is None or not ENGINE_JOB.done(): return
    mode=ENGINE_MODE
    try:
        ENGINE_JOB.result()
        log(f'PAPER job completed: {mode}')
        record_request(ENGINE_REQUEST_ID,'completed')
    except Exception as e:
        log(f'PAPER job failed [{mode}]: {type(e).__name__}')
        record_request(ENGINE_REQUEST_ID,'execution_failed',error=type(e).__name__)
        if mode!='monitor': telegram('작업을 완료하지 못했습니다. /status에서 상태를 확인하세요.')
    ENGINE_JOB=None; ENGINE_MODE=None; ENGINE_REQUEST_ID=None


def run_engine(mode,snapshot_asof=None):
    script='magi2/refill_engine.py' if mode=='refill' else 'magi2/paper_engine.py'
    args=[sys.executable,script] if mode=='refill' else [sys.executable,script,mode]
    if mode in ('morning','rebuild') and snapshot_asof: args+=['--snapshot-asof',snapshot_asof]
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


def consume_startup_rebalance():
    """Operator-authorized PAPER request, unique ID and expiry, at most once.

    The environment variable is set only for an explicitly requested run.
    Claim before starting; a restart never automatically retries a claimed run.
    """
    raw=os.getenv('MAGI2_REBALANCE_ONCE','').strip()
    if not raw: return
    try:
        request=json.loads(raw); ident=request['id']
        expires=datetime.fromisoformat(request['expires_at'])
        if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident): raise ValueError('Invalid request ID')
        if expires.tzinfo is None or not 0<(expires-datetime.now(KST)).total_seconds()<=86400:
            log('VPD one-shot ignored: expired or invalid deadline'); return
        if load_json(ROOT/'magi2/config.json').get('mode')!='PAPER': raise ValueError('PAPER required')
        marker=STATE_DIR/'rebalance_requests.json'
        if marker.exists() and ident in load_json(marker):
            log(f'VPD one-shot already consumed: {ident}'); return
        record_request(ident,'claimed',expires_at=expires.isoformat())
        if not start_engine('morning',request_id=ident): record_request(ident,'not_started_busy')
    except (OSError,ValueError,KeyError,TypeError) as e:
        log(f'VPD one-shot rejected: {type(e).__name__}')


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


def signals_text(view='signals'):
    path=os.getenv('MAGI1_INTELLIGENCE_PATH','').strip()
    url=os.getenv('MAGI1_INTELLIGENCE_URL','').strip()
    if not path and not url:
        return '⚡ FAST · Whale 신호\n관측 파일 전달 경로가 아직 연결되지 않았습니다. 신호가 없다는 뜻은 아닙니다.'
    try:
        rows=(load_intelligence(path,int(time.time()*1000)) if path else
              fetch_intelligence(url,os.getenv('MAGI_INTELLIGENCE_TOKEN',''),int(time.time()*1000)))
    except (OSError,ValueError,KeyError,TypeError,requests.RequestException):
        return '⚡ FAST · Whale 신호\n최신 관측을 확인할 수 없습니다. 파일 누락·만료·형식을 점검해야 합니다.'
    return observation_text(rows,view)


def may_execute(chat_id,user_id):
    if str(chat_id)!=ALLOWED_CHAT_ID: return False
    if ALLOWED_USER_IDS: return str(user_id) in ALLOWED_USER_IDS
    return str(user_id)==str(chat_id) and not str(chat_id).startswith('-')


def handle_command(text,chat_id=None,user_id=None):
    started=time.monotonic()
    cmd=parse_command(text,BOT_USERNAME)
    chat_id=str(chat_id or ALLOWED_CHAT_ID)
    try:
        if cmd in ('help','menu'):
            telegram(help_text() if cmd=='help' else '📋 MAGI 메뉴\n시장 관측 · 전략 검증 · 자산 관리\n실계좌 조회·PAPER·Shadow를 구분해 표시합니다.\n역할별 안내는 🧩 MAGI 역할을 누르세요.',main_keyboard())
        elif cmd=='vpd':
            telegram('📊 VPD 모의투자\n가상자금으로 운영하는 PAPER 계정입니다.\n'
                     '현황 보고: 보유 종목·손익 조회\nVPD 조회: 오전·저녁 분석 저장본\n리밸런싱: 요청 시점 새 스캔 후 보유 종목 재조정\n종목 리필: 빈자리 채우기\n전량 교체: 모두 매도 후 새 VPD TOP10으로 균등 재구성\n'
                     '실행 버튼을 누르면 먼저 확인 화면이 열립니다.',vpd_keyboard())
        elif cmd=='about': telegram(role_text(),role_keyboard())
        elif cmd=='scan': telegram('🔎 어떤 VPD 저장본을 조회할까요?',scan_keyboard())
        elif cmd=='morning_scan': return_magi1_state('morning')
        elif cmd=='evening_scan': return_magi1_state('evening')
        elif cmd=='report':
            if not start_engine('report'): telegram('다른 PAPER 작업이 진행 중입니다. 잠시 후 다시 조회하세요.')
            else: telegram('📊 VPD 현황을 조회하고 있습니다. 결과를 곧 보내드리겠습니다.')
        elif cmd in ('morning','refill','rebuild'):
            if not may_execute(chat_id,user_id):
                telegram('실행 권한이 없는 사용자입니다. 조회 메뉴는 이용할 수 있습니다.'); return
            if SCAN_JOB is not None or (ENGINE_JOB is not None and not ENGINE_JOB.done()):
                telegram('PAPER 작업이 진행 중입니다. 완료 후 다시 선택하세요.'); return
            if cmd=='rebuild':
                telegram('🔁 VPD 전량 교체 · 모의투자\n'
                         '① 요청 시점 기준 VPD를 새로 조회\n'
                         '② 기존 보유분 전량 매도\n'
                         '③ 매도 후 총 가용금액으로 새 TOP10 균등 매수\n\n'
                         '보유 유지·약화 유예·당일 재진입 제한을 적용하지 않습니다. 기존 종목도 TOP10에 들면 다시 매수합니다.\n'
                         '모의 수수료·슬리피지가 반영되며 최초 원금과 누적 손익·원장은 보존합니다.\n'
                         '60초 안에 아래 버튼으로 확인하세요. 실제 계좌 주문은 발생하지 않습니다.',
                         CONFIRMATIONS.issue(cmd,chat_id,user_id)); return
            label='리밸런싱 (기존 보유 종목의 모의 매도·매수 가능)' if cmd=='morning' else '종목 리필 (빈자리 모의 신규 매수)'
            telegram(f'PAPER {label}을 실행할까요?\n60초 안에 확인하세요. 실제 주문은 발생하지 않습니다.',
                     CONFIRMATIONS.issue(cmd,chat_id,user_id))
        elif cmd=='cancel':
            CONFIRMATIONS.cancel(chat_id,user_id)
            telegram('대기 중인 실행 확인을 취소했습니다. 이미 진행 중인 작업은 계속됩니다.')
        elif cmd=='status':
            telegram('🤖 시스템 상태 — 확인할 MAGI를 선택하세요.',status_keyboard())
        elif cmd=='status1':
            path=os.getenv('MAGI1_INTELLIGENCE_PATH','').strip()
            url=os.getenv('MAGI1_INTELLIGENCE_URL','').strip()
            try:
                rows=(load_intelligence(path,int(time.time()*1000)) if path else
                      fetch_intelligence(url,os.getenv('MAGI_INTELLIGENCE_TOKEN',''),int(time.time()*1000)))
                message=f'최신 관측 인터페이스: 정상\n최근 관측: {len(rows)}건'
            except (OSError,ValueError,KeyError,TypeError,requests.RequestException):
                message='최신 관측 인터페이스: 확인 불가 (연결·만료·형식 점검 필요)'
            telegram('MAGI1 · 시세·관측 상태\n'+message+'\n개별 거래소 수집기 상태와 전체 데이터 품질은 이 조회만으로 판정하지 않습니다.',status_keyboard())
        elif cmd=='status2':
            running=ENGINE_MODE if ENGINE_JOB is not None and not ENGINE_JOB.done() else '대기'
            if SCAN_JOB is not None: running=f'VPD 스캔 중 ({SCAN_CONTEXT["asof"]}) / '+str(running)
            telegram(f'MAGI2 • 전략 • 검증 상태\n텔레그램: 응답 중\n실행 계정: PAPER\nPAPER 작업: {running}\n자동 모니터 간격: {INTERVAL}초',status_keyboard())
        elif cmd in ('magi3','execution','status3'): telegram(execution_view('magi3' if cmd=='status3' else cmd),status_keyboard())
        elif cmd=='shadows':
            telegram('🧪 shadows 모의투자\n현재 가상자산·손익과 최근 72시간 매매이력을 확인하세요.\n조회만 수행하며 모의매매를 시작하지 않습니다.',shadow_keyboard())
        elif cmd=='shadow': telegram(execution_view(cmd),shadow_keyboard())
        elif cmd=='orders': send_shadow_orders()
        elif cmd=='assets': telegram(execution_view(cmd))
        elif cmd=='strategies': telegram(validation_text(),strategy_keyboard())
        elif cmd=='regime': start_regime_job()
        elif cmd=='wave': send_wave()
        elif cmd in ('fast_report','fast_orders','fast_daily','fast_balance'):
            from magi2.fast_paper_report import view
            telegram(*view(FAST_PAPER.ledger if FAST_PAPER else None,time.time_ns()//1000000,cmd))
        elif cmd=='fast_replay':
            from magi2.fast_report import view
            root=os.getenv('FAST_PAPER_REPORT_DIR',str(STATE_DIR/'fast'))
            telegram(*view(root))
        elif cmd=='fast_compare':
            from magi2.fast_comparison import report
            telegram(report(FAST_MONITOR.audit,time.time_ns()//1000000) if FAST_MONITOR else 'FAST 검증 자료 준비 중입니다.',strategy_keyboard(detail=True,fast=True))
        elif cmd in ('signals','fast'):
            if FAST_MONITOR: telegram(*FAST_MONITOR.captures())
            else: telegram('FAST 포착 자료 준비 중입니다.')
        elif text.strip(): telegram('명령을 찾지 못했습니다. /help 또는 아래 버튼을 이용하세요.',main_keyboard())
    except Exception as e:
        log(f'Command failed [{cmd}]: {type(e).__name__}')
        telegram('명령을 처리하지 못했습니다. 잠시 후 다시 시도하세요.')
    finally:
        log(f'telegram_command command={cmd or "unknown"} handler_ms={round((time.monotonic()-started)*1000)}')


def send_wave(section='overview',offset=0):
    telegram(*(WAVE_CLIENT.view(section,offset) if WAVE_CLIENT else render_wave(None,section,offset)))


def send_shadow_orders(until=None,offset=0):
    from magi2.execution_client import orders_page
    text,markup=orders_page(until,offset)
    telegram(text,markup)


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
    if data.startswith('wave:'):
        try:
            _,section,offset=data.split(':')
            offset=int(offset)
        except ValueError:return
        if section in ('overview','strength','evidence','trading','guide') and 0<=offset<=10000:
            send_wave(section,offset)
    elif data.startswith('captures:'):
        try:offset=max(0,int(data.split(':')[1]))
        except ValueError:return
        if FAST_MONITOR:telegram(*FAST_MONITOR.captures(offset))
    elif data.startswith('orders:'):
        try:
            _,until,offset=data.split(':')
            send_shadow_orders(int(until),int(offset))
        except ValueError: telegram('이력 버튼이 만료되었거나 잘못되었습니다. 최근 3일 매매이력을 다시 선택하세요.',shadow_keyboard())
    elif data.startswith('guide:'):
        name=data[6:]
        if name=='wave':
            send_wave()
        elif name in ('vpd','fast','basis','cross'):
            telegram(strategy_text(name),strategy_keyboard(detail=True,fast=name=='fast'))
    elif data.startswith('nav:'):
        command=data[4:]
        if command in ('morning_scan','evening_scan','menu','help','about','status','status1','status2','status3','fast','fast_report','fast_orders','fast_daily','fast_balance','fast_replay','fast_compare','wave','scan','report','assets','shadow','shadows','orders','vpd','morning','refill','rebuild','strategies','regime'):
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


def get_updates(offset,timeout=LONG_POLL_SECONDS):
    # Telegram returns immediately when an update arrives; timeout is idle wait only.
    r=TELEGRAM_HTTP.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',
        params={'offset':offset,'timeout':timeout,'allowed_updates':json.dumps(['message','callback_query'])},
        timeout=(5,timeout+10))
    r.raise_for_status(); data=r.json()
    if not data.get('ok',True): raise RuntimeError('TELEGRAM_POLL_REJECTED')
    return data.get('result',[])


def poll_timeout(next_monitor,next_probe,engine_busy=False):
    remaining=min(next_monitor,next_probe)-time.monotonic()
    return max(1,min(LONG_POLL_SECONDS,int(remaining),1 if engine_busy else LONG_POLL_SECONDS))


def execution_probe():
    try: log('magi3_probe: '+execution_view('execution').replace('\n',' | '))
    except Exception as exc: log(f'magi3_probe_failed: {type(exc).__name__}')


def discard_pending_updates():
    offset=0
    try:
        updates=get_updates(0,timeout=0)
        if updates:
            offset=max(int(x['update_id']) for x in updates)+1; get_updates(offset,timeout=0); log(f'Discarded {len(updates)} pending Telegram update(s) on startup')
    except Exception as e: log(f'Telegram startup flush error: {type(e).__name__}')
    return offset


def poll_updates(offset,timeout=LONG_POLL_SECONDS):
    try:
        for update in get_updates(offset,timeout=timeout):
            ident=int(update['update_id'])
            if ident<offset: continue
            offset=ident+1
            if 'callback_query' in update:
                handle_callback(update['callback_query']); continue
            msg=update.get('message') or {}
            chat_id=str((msg.get('chat') or {}).get('id',''))
            if chat_id!=ALLOWED_CHAT_ID: continue
            handle_command(msg.get('text') or '',chat_id,str((msg.get('from') or {}).get('id','')))
    except Exception as e:
        log(f'Telegram polling error: {type(e).__name__}')
        time.sleep(1)  # Back off on failures only; no healthy-path polling sleep.
    return offset


def main():
    global FAST_MONITOR,FAST_PAPER,WAVE_CLIENT
    prepare_persistent_state()
    WAVE_CLIENT=WaveClient(os.getenv('MAGI1_INTELLIGENCE_URL',''),
                           os.getenv('MAGI_INTELLIGENCE_TOKEN',''),log).start()
    start_shadow_bridge(STATE_DIR,GITHUB_REPO,log)
    if not BOT_TOKEN or not ALLOWED_CHAT_ID: raise RuntimeError('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.')
    try: backup_paper_history()
    except Exception as e: log(f'Initial GitHub PAPER backup error (engine unaffected): {e}')
    try: setup_telegram_menu()
    except Exception as e: log(f'Telegram menu registration failed: {type(e).__name__}')
    try: refresh_telegram_keyboard()
    except Exception as e: log(f'Telegram keyboard refresh failed: {type(e).__name__}')
    if os.getenv('MAGI1_INTELLIGENCE_URL') or os.getenv('MAGI1_INTELLIGENCE_PATH'):
        log('intelligence_probe: '+signals_text('fast').replace('\n',' | ')[:1200])
    offset=discard_pending_updates(); log(f'MAGI Railway authority started; monitor={INTERVAL}s; Telegram console=ON')
    from magi2.fast_volume_monitor import FastVolumeMonitor as FastMonitor
    from magi2.fast_tick_service import TickPaperService as PaperService
    FAST_PAPER=PaperService(STATE_DIR,log).start()
    FAST_MONITOR=FastMonitor(STATE_DIR,log,paper=FAST_PAPER);FAST_MONITOR.start()
    consume_startup_rebalance()
    next_monitor=time.monotonic()+INTERVAL
    next_execution_probe=time.monotonic()+30
    probe_job=None
    log(f'Telegram responsive polling active: long_poll={LONG_POLL_SECONDS}s; fixed_sleep=0; probe=background; notification_poll_cap=1s')
    while True:
        if time.monotonic()>=next_execution_probe:
            if os.getenv('MAGI3_SERVICE_URL') and (probe_job is None or probe_job.done()):
                probe_job=PROBE_EXECUTOR.submit(execution_probe)
            next_execution_probe=time.monotonic()+300
        for event in FAST_MONITOR.drain(): telegram(event['text'])
        try: FAST_PAPER.daily(lambda text:telegram_api('sendMessage',{'chat_id':ALLOWED_CHAT_ID,'text':text}))
        except Exception as exc: log(f'fast_paper_daily_error type={type(exc).__name__}')
        finish_engine_job()
        finish_scan_job()
        finish_regime_job()
        if time.monotonic()>=next_monitor:
            next_monitor=time.monotonic()+(INTERVAL if start_engine('monitor') else 1)
        timeout=poll_timeout(next_monitor,next_execution_probe,ENGINE_JOB is not None)
        offset=poll_updates(offset,timeout=min(timeout,1))

if __name__=='__main__': main()
