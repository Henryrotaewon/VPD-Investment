"""Daily KST PAPER job claim. A claimed date is never replayed after a restart."""
from datetime import timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
TIME = '07:30'
GRACE_MINUTES = 15


def due(now):
    if now.tzinfo is None:
        raise ValueError('AWARE_TIME_REQUIRED')
    local = now.astimezone(KST)
    scheduled = local.replace(hour=7, minute=30, second=0, microsecond=0)
    return scheduled if scheduled <= local < scheduled+timedelta(minutes=GRACE_MINUTES) else None


def claim(root, now):
    scheduled = due(now)
    if scheduled is None:
        return None
    path = Path(root)/'vpd_schedule.sqlite3'
    db = sqlite3.connect(path,timeout=1)
    ident = 'vpd-daily-0730-'+scheduled.date().isoformat()
    try:
        db.execute('CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, scheduled_at TEXT, claimed_at TEXT)')
        with db:
            result = db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?)',
                                (ident,scheduled.isoformat(),now.isoformat()))
        return ident if result.rowcount == 1 else None
    finally:
        db.close()
