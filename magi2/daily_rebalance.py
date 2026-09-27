"""Local PAPER schedule; one durable claim per KST date, no catch-up/retry."""
from datetime import timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


class DailyRebalance:
    def __init__(self, root, config, log, notify):
        self.settings = config.get('daily_rebalance', {})
        self.enabled = bool(self.settings.get('enabled')) and config.get('mode') == 'PAPER'
        self.hour = int(self.settings.get('hour', 7))
        self.minute = int(self.settings.get('minute', 20))
        if not 0 <= self.hour < 24 or not 0 <= self.minute < 60:
            raise ValueError('INVALID_REBALANCE_SCHEDULE')
        self.log, self.notify = log, notify
        self.db = sqlite3.connect(Path(root)/'vpd_schedule.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS claims(day TEXT PRIMARY KEY, request_id TEXT, status TEXT)')
        self.db.commit()

    def next_run(self, now):
        local = now.astimezone(KST)
        target = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if local >= target + timedelta(minutes=1) or self.db.execute('SELECT 1 FROM claims WHERE day=?', (target.date().isoformat(),)).fetchone():
            target += timedelta(days=1)
        return target

    def tick(self, now, start):
        if not self.enabled:
            return False
        if now.tzinfo is None:
            raise ValueError('Timezone required')
        local = now.astimezone(KST)
        target = local.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        if not target <= local < target + timedelta(minutes=1):
            return False
        day = target.date().isoformat()
        ident = 'scheduled-vpd-' + day
        # Commit BEFORE dispatch. A crash, busy worker, failed scan, or restart
        # must never trigger a second daily attempt (including at 07:30).
        with self.db:
            claimed = self.db.execute('INSERT OR IGNORE INTO claims VALUES(?,?,?)', (day,ident,'claimed')).rowcount
        if not claimed:
            return False
        self.log(f'vpd_schedule_claimed request={ident} asof={target.isoformat()}')
        try:
            accepted = start(ident, target)
            status = 'started' if accepted else 'not_started_busy'
        except Exception as exc:
            status = 'start_failed'
            self.log(f'vpd_schedule_error request={ident} type={type(exc).__name__}')
        with self.db:
            self.db.execute('UPDATE claims SET status=? WHERE day=?', (status,day))
        self.log(f'vpd_schedule_dispatch request={ident} status={status} retry=false')
        if status != 'started':
            self.notify('VPD 07:20 자동 리밸런싱을 시작하지 못했습니다. 오늘 자동 재호출은 하지 않습니다. 필요하면 수동 리밸런싱을 이용하세요.')
        return status == 'started'
