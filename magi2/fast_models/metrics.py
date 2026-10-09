"""Streaming drawdowns and 09:00 KST daily comparisons, including data gaps."""
import json

DAY = 86_400_000
INITIAL = 3_000_000.
VERSION = 'fast-models-20261009-v1'


class Metrics:
    def __init__(self, db, started):
        self.db = db
        db.executescript('''
          CREATE TABLE IF NOT EXISTS model_metrics(id INTEGER PRIMARY KEY,payload TEXT);
          CREATE TABLE IF NOT EXISTS model_days(day INTEGER PRIMARY KEY,payload TEXT);
          CREATE TABLE IF NOT EXISTS model_trades(id TEXT PRIMARY KEY,ts INTEGER,kind TEXT,pnl REAL,payload TEXT);
        ''')
        row = db.execute('SELECT payload FROM model_metrics WHERE id=1').fetchone()
        self.s = json.loads(row[0]) if row else dict(started_ms=started, initial=INITIAL,
            peak=INITIAL, mdd=0., last_ms=started, equity=INITIAL, valid=True, gap=False)
        if not row:
            self.mark(started, INITIAL, True)

    def mark(self, ts, equity, valid):
        if ts < self.s['last_ms']:
            return
        day = ts//DAY
        r = self.db.execute('SELECT payload FROM model_days WHERE day=?', (day,)).fetchone()
        if r:
            d = json.loads(r[0])
        else:
            first = day == self.s['started_ms']//DAY
            anchor = INITIAL if first else self.s['equity'] if (
                self.s['valid'] and 0 <= day*DAY-self.s['last_ms'] <= 60_000) else None
            d = dict(day=day, base=anchor, peak=anchor, mdd=0., samples=0, invalid=0, gap=not first and anchor is None)
        gap = ts-self.s['last_ms'] > 60_000
        d['gap'] |= gap or not valid
        self.s['gap'] |= gap or not valid
        if valid and equity is not None:
            d['peak'] = max(d['peak'] if d['peak'] is not None else equity, equity)
            d['mdd'] = max(d['mdd'], (1-equity/d['peak'])*100 if d['peak']>0 else 0.)
            self.s['peak'] = max(self.s['peak'], equity)
            self.s['mdd'] = max(self.s['mdd'], (1-equity/self.s['peak'])*100)
            self.s['equity'] = equity
        d.update(ts=ts, equity=equity, valid=valid, samples=d['samples']+1,
                 invalid=d['invalid']+int(not valid))
        self.s.update(last_ms=ts, valid=valid)
        self.db.execute('INSERT OR REPLACE INTO model_days VALUES(?,?)', (day, json.dumps(d, allow_nan=False)))
        self.db.execute('INSERT OR REPLACE INTO model_metrics VALUES(1,?)', (json.dumps(self.s, allow_nan=False),))

    def trade(self, ident, ts, kind, pnl, details):
        self.db.execute('INSERT OR IGNORE INTO model_trades VALUES(?,?,?,?,?)',
                        (ident, ts, kind, pnl, json.dumps(details, allow_nan=False)))
