import os
import time
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
INTERVAL = int(os.getenv('MAGI2_MONITOR_INTERVAL_SECONDS', '60'))


def log(msg):
    print(f"[{datetime.now(KST).isoformat()}] {msg}", flush=True)


def run_monitor():
    proc = subprocess.run(
        ['python', 'magi2/paper_engine.py', 'monitor'],
        capture_output=True,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end='', flush=True)
    if proc.stderr:
        print(proc.stderr, end='', flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f'MAGI2 monitor failed with exit code {proc.returncode}')


def main():
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
