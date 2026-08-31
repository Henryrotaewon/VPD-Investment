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

ROOT = Path(__file__).resolve().parents[1]
REPO_STATE_DIR = ROOT / 'magi2' / 'state'
KST = ZoneInfo('Asia/Seoul')
INTERVAL = int(os.getenv('MAGI2_MONITOR_INTERVAL_SEC', os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS', '60')))
POLL_SECONDS = 5
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
ALLOWED_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()
MAGI1_GITHUB_TOKEN = os.getenv('MAGI1_GITHUB_TOKEN', '').strip()
GITHUB_REPO = os.getenv('MAGI_GITHUB_REPO', 'Henryrotaewon/VPD-Investment').strip()


def log(msg):
    print(f'[{datetime.now(KST).isoformat()}] {msg}', flush=True)


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def assert_no_recovery_regression(seed_file, live_file):
    """Fail closed if a trade known CLOSED in the recovery seed is OPEN again."""
    seed = load_json(seed_file)
    live = load_json(live_file)
    conflicts = []
    for coin, seed_pos in seed.get('positions', {}).items():
        if seed_pos.get('status') != 'CLOSED':
            continue
        live_pos = live.get('positions', {}).get(coin)
        if not live_pos:
            continue
        same_trade = live_pos.get('entry_at') == seed_pos.get('entry_at')
        if same_trade and live_pos.get('status', 'OPEN') == 'OPEN':
            conflicts.append(coin)
    if conflicts:
        raise RuntimeError('RECOVERY CONFLICT: previously CLOSED trade is OPEN: ' + ', '.join(conflicts))


def prepare_persistent_state():
    configured = os.getenv('MAGI2_STATE_DIR') or os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    volume_dir = Path(configured) if configured else Path('/data/magi2')
    on_railway = bool(os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_PROJECT_ID'))

    if not configured and not volume_dir.exists():
        if on_railway:
            raise RuntimeError('Railway persistent volume is not mounted. MAGI2 refuses to start.')
        log(f'Persistent volume not mounted; local run uses repository state: {REPO_STATE_DIR}')
        return REPO_STATE_DIR

    volume_dir.mkdir(parents=True, exist_ok=True)
    REPO_STATE_DIR.mkdir(parents=True, exist_ok=True)
    seed_state = REPO_STATE_DIR / 'paper_state.json'

    for name in ('paper_state.json', 'paper_events.jsonl'):
        repo_file = REPO_STATE_DIR / name
        volume_file = volume_dir / name
        if not volume_file.exists():
            if repo_file.exists() and not repo_file.is_symlink():
                shutil.copy2(repo_file, volume_file)
                log(f'Bootstrapped {name} -> {volume_file}')
            elif name == 'paper_events.jsonl':
                volume_file.touch()
            else:
                raise RuntimeError(f'Cannot bootstrap missing MAGI2 state file: {repo_file}')

    # Validate before replacing the repository path with symlinks.
    if seed_state.exists() and not seed_state.is_symlink():
        assert_no_recovery_regression(seed_state, volume_dir / 'paper_state.json')

    for name in ('paper_state.json', 'paper_events.jsonl'):
        repo_file = REPO_STATE_DIR / name
        volume_file = volume_dir / name
        if repo_file.is_symlink() or repo_file.exists():
            repo_file.unlink()
        repo_file.symlink_to(volume_file)

    log(f'MAGI2 persistent state active: {volume_dir}')
    return volume_dir


def telegram(text):
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': ALLOWED_CHAT_ID, 'text': text},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        log(f'Telegram send error: {e}')


def run_engine(mode):
    proc = subprocess.run(
        [sys.executable, 'magi2/paper_engine.py', mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if proc.stdout:
        print(proc.stdout, end='', flush=True)
    if proc.stderr:
        print(proc.stderr, end='', flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f'MAGI2 {mode} failed with exit code {proc.returncode}')


def dispatch_magi1(session):
    if not MAGI1_GITHUB_TOKEN:
        telegram('⚠️ MAGI1 명령 준비중\nRailway의 MAGI1_GITHUB_TOKEN 설정이 필요합니다.')
        return
    url = f'https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/vpd-scan.yml/dispatches'
    r = requests.post(
        url,
        headers={
            'Authorization': f'Bearer {MAGI1_GITHUB_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        json={'ref': 'main', 'inputs': {'session': session}},
        timeout=20,
    )
    r.raise_for_status()
    telegram(f'🛰️ MAGI1 {session.upper()} SCAN\nGitHub Actions 실행 요청 완료')


def help_text():
    return (
        '🤖 MAGI Command Manual\n\n'
        'MAGI1\n'
        'magi1 morning scan\n'
        'magi1 evening scan\n\n'
        'MAGI2\n'
        'magi2 morning\n'
        'magi2 refill\n'
        'magi2 report\n'
        'magi2 help\n\n'
        'MAGI2 Monitor는 Railway에서 자동 실행 · PAPER ONLY'
    )


def handle_command(text):
    cmd = ' '.join(text.strip().lower().split())
    try:
        if cmd == 'magi1 morning scan':
            dispatch_magi1('morning')
        elif cmd == 'magi1 evening scan':
            dispatch_magi1('evening')
        elif cmd == 'magi2 morning':
            run_engine('morning')
        elif cmd == 'magi2 refill':
            run_engine('refill')
        elif cmd == 'magi2 report':
            run_engine('report')
        elif cmd == 'magi2 help':
            telegram(help_text())
        elif cmd.startswith('magi1') or cmd.startswith('magi2'):
            telegram('❓ 알 수 없는 MAGI 명령입니다.\n`magi2 help`로 명령어를 확인하세요.')
        else:
            return
    except Exception as e:
        log(f'Command failed [{cmd}]: {e}')
        telegram(f'🚨 MAGI 명령 실패\n{cmd}\n{e}')


def poll_updates(offset):
    if not BOT_TOKEN:
        return offset
    try:
        r = requests.get(
            f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates',
            params={'offset': offset, 'timeout': 0, 'allowed_updates': json.dumps(['message'])},
            timeout=10,
        )
        r.raise_for_status()
        for update in r.json().get('result', []):
            offset = max(offset, int(update['update_id']) + 1)
            msg = update.get('message') or {}
            chat_id = str((msg.get('chat') or {}).get('id', ''))
            text = msg.get('text') or ''
            if chat_id != ALLOWED_CHAT_ID:
                log(f'Ignored Telegram command from unauthorized chat {chat_id}')
                continue
            handle_command(text)
    except Exception as e:
        log(f'Telegram polling error: {e}')
    return offset


def main():
    prepare_persistent_state()
    if not BOT_TOKEN or not ALLOWED_CHAT_ID:
        raise RuntimeError('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.')
    log(f'MAGI Railway authority started; monitor={INTERVAL}s; Telegram console=ON')
    offset = 0
    next_monitor = time.monotonic() + INTERVAL  # startup grace: commands/state can be checked first
    while True:
        offset = poll_updates(offset)
        now = time.monotonic()
        if now >= next_monitor:
            try:
                run_engine('monitor')
            except Exception as e:
                log(f'Monitor error: {e}')
            next_monitor = time.monotonic() + INTERVAL
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()
