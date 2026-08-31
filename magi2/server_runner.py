import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
REPO_STATE_DIR = ROOT / 'magi2' / 'state'
KST = ZoneInfo('Asia/Seoul')
INTERVAL = int(
    os.getenv(
        'MAGI2_MONITOR_INTERVAL_SEC',
        os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS', '60'),
    )
)


def log(msg):
    print(f"[{datetime.now(KST).isoformat()}] {msg}", flush=True)


def prepare_persistent_state():
    """Route MAGI2 state files to the Railway persistent volume when mounted.

    The repository copy is only a bootstrap seed. Once the volume contains a
    state file, it is never overwritten by a deployment checkout.
    """
    configured = os.getenv('MAGI2_STATE_DIR') or os.getenv('RAILWAY_VOLUME_MOUNT_PATH')
    volume_dir = Path(configured) if configured else Path('/data/magi2')

    # Local/non-volume runs keep using the repository state directory.
    if not configured and not volume_dir.exists():
        log(f'Persistent volume not mounted; using repository state: {REPO_STATE_DIR}')
        return

    volume_dir.mkdir(parents=True, exist_ok=True)
    REPO_STATE_DIR.mkdir(parents=True, exist_ok=True)

    for name in ('paper_state.json', 'paper_events.jsonl'):
        repo_file = REPO_STATE_DIR / name
        volume_file = volume_dir / name

        # Bootstrap an empty volume from the checked-out recovery copy only once.
        if not volume_file.exists():
            if repo_file.exists() and not repo_file.is_symlink():
                shutil.copy2(repo_file, volume_file)
                log(f'Bootstrapped {name} -> {volume_file}')
            elif name == 'paper_events.jsonl':
                volume_file.touch()
            else:
                raise RuntimeError(f'Cannot bootstrap missing MAGI2 state file: {repo_file}')

        # paper_engine.py keeps its original paths, but those paths now resolve
        # directly to persistent storage.
        if repo_file.is_symlink() or repo_file.exists():
            repo_file.unlink()
        repo_file.symlink_to(volume_file)

    log(f'MAGI2 persistent state active: {volume_dir}')


def run_monitor():
    proc = subprocess.run(
        [sys.executable, 'magi2/paper_engine.py', 'monitor'],
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
        raise RuntimeError(f'MAGI2 monitor failed with exit code {proc.returncode}')


def main():
    prepare_persistent_state()
    log(f'MAGI2 server runner started; interval={INTERVAL}s')
    while True:
        started = time.monotonic()
        try:
            run_monitor()
        except Exception as e:
            log(f'ERROR: {e}')
        elapsed = time.monotonic() - started
        time.sleep(max(1, INTERVAL - elapsed))


if __name__ == '__main__':
    main()
