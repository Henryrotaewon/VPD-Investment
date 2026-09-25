"""Version/cohort-isolated SHADOW evidence. Never opens legacy FAST paper files."""
import json
from pathlib import Path
import re
import sqlite3
from magi2.fast_wave_indicator import VERSION


def state_root(root, cohort):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', cohort):
        raise ValueError('INVALID_COHORT')
    target = Path(root) / 'fast_wave' / VERSION / cohort
    target.mkdir(parents=True, exist_ok=True)
    return target


class EvidenceStore:
    def __init__(self, root, cohort):
        self.cohort = cohort
        self.path = state_root(root, cohort) / 'observations.sqlite3'
        self.db = sqlite3.connect(self.path)
        self.db.execute('CREATE TABLE IF NOT EXISTS observations ('
                        'venue TEXT, symbol TEXT, observed_ms INTEGER, payload TEXT NOT NULL, '
                        'PRIMARY KEY(venue,symbol,observed_ms))')
        self.db.execute('CREATE TABLE IF NOT EXISTS evaluations ('
                        'venue TEXT, symbol TEXT, observed_ms INTEGER, payload TEXT NOT NULL, '
                        'PRIMARY KEY(venue,symbol,observed_ms))')
        self.db.execute('CREATE TABLE IF NOT EXISTS errors ('
                        'id INTEGER PRIMARY KEY, venue TEXT, symbol TEXT, observed_ms INTEGER, reason TEXT)')
        self.db.commit()

    def close(self):
        self.db.close()

    def record_error(self, venue, symbol, observed_ms, reason):
        with self.db:
            self.db.execute('INSERT INTO errors(venue,symbol,observed_ms,reason) VALUES(?,?,?,?)',
                            (venue, symbol, observed_ms, reason[:200]))

    def append(self, point):
        if point['strategy_version'] != VERSION:
            raise ValueError('WRONG_STRATEGY_VERSION')
        payload = json.dumps(point, allow_nan=False, sort_keys=True)
        with self.db:
            old = self.db.execute('SELECT payload FROM observations WHERE venue=? AND symbol=? AND observed_ms=?',
                                  (point['venue'], point['symbol'], point['observed_ms'])).fetchone()
            if old and old[0] != payload:
                raise ValueError('CONFLICTING_OBSERVATION')
            self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?)',
                            (point['venue'], point['symbol'], point['observed_ms'], payload))

    def recent(self, venue, symbol, now_ms):
        return [json.loads(row[0]) for row in reversed(self.db.execute(
            'SELECT payload FROM observations WHERE venue=? AND symbol=? AND observed_ms<=? '
            'ORDER BY observed_ms DESC LIMIT 3', (venue, symbol, now_ms)).fetchall())]

    def record(self, point, result):
        if result['strategy_version'] != VERSION or result['mode'] != 'SHADOW':
            raise ValueError('WRONG_EVALUATION_MODE')
        payload = json.dumps(dict(result, cohort=self.cohort), allow_nan=False, sort_keys=True)
        with self.db:
            # Retry cannot rewrite the first recorded decision for a snapshot.
            self.db.execute('INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?)',
                            (point['venue'], point['symbol'], point['observed_ms'], payload))
