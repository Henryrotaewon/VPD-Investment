"""Compact, exact five-minute turnover for additional FAST venues.

Each day contains 288 doubles; NaN denotes unknown, and zero denotes a
validated no-trade interval. Legacy rows remain readable during migration.
"""
from collections import defaultdict
import math
import struct
import zlib

DAY = 86400000
FIVE = 300000
_enabled = set()


def enable(db):
    db.execute('CREATE TABLE IF NOT EXISTS turnover_days(symbol TEXT,day INTEGER,values_blob BLOB,PRIMARY KEY(symbol,day))')
    _enabled.add(db)


def unpack(blob):
    return list(struct.unpack('<288d', zlib.decompress(blob)))


def days(db, symbol):
    if db not in _enabled:
        return {}
    return {day: unpack(blob) for day, blob in db.execute(
        'SELECT day,values_blob FROM turnover_days WHERE symbol=?', (symbol,))}


def historical(db, symbol, keys):
    if db not in _enabled:
        return {}
    dates = sorted({t // DAY * DAY for t in keys})
    rows = db.execute('SELECT day,values_blob FROM turnover_days WHERE symbol=? AND day IN ('
                      + ','.join('?' for _ in dates) + ')', (symbol, *dates))
    packed = {day: unpack(blob) for day, blob in rows}
    out = {}
    for t in keys:
        values = packed.get(t // DAY * DAY)
        if values is not None and math.isfinite(values[t % DAY // FIVE]):
            out[t] = values[t % DAY // FIVE]
    return out


def known(db, symbol, start, end):
    return {day + i * FIVE for day, values in days(db, symbol).items()
            for i, value in enumerate(values)
            if math.isfinite(value) and start <= day + i * FIVE < end}


def compact(db, cutoff, batch_size=4096):
    if db not in _enabled:
        return
    # Bound memory and WAL work on the live event-loop writer. Remaining legacy
    # rows are still read by reference_state and subsequent batches migrate them.
    batch = db.execute('SELECT symbol,bucket,value FROM baseline ORDER BY symbol,bucket LIMIT ?',
                       (batch_size,)).fetchall()
    grouped = defaultdict(list)
    for symbol, bucket, value in batch:
        grouped[symbol, bucket // DAY * DAY].append((bucket % DAY // FIVE, value))
    db.execute('SAVEPOINT compact_turnover')
    try:
        for (symbol, day), rows in grouped.items():
            old = db.execute('SELECT values_blob FROM turnover_days WHERE symbol=? AND day=?', (symbol, day)).fetchone()
            values = unpack(old[0]) if old else [float('nan')] * 288
            for slot, value in rows:
                if not math.isfinite(value) or value < 0:
                    raise ValueError('INVALID_TURNOVER_MIGRATION')
                values[slot] = value
            encoded = struct.pack('<288d', *values)
            blob = zlib.compress(encoded)
            if zlib.decompress(blob) != encoded:
                raise ValueError('TURNOVER_ROUNDTRIP_FAILED')
            db.execute('INSERT OR REPLACE INTO turnover_days VALUES(?,?,?)', (symbol, day, blob))
        db.executemany('DELETE FROM baseline WHERE symbol=? AND bucket=?',
                       [(symbol, bucket) for symbol, bucket, _ in batch])
        db.execute('DELETE FROM turnover_days WHERE day<?', ((cutoff - FIVE - 10 * DAY) // DAY * DAY,))
    except BaseException:
        db.execute('ROLLBACK TO compact_turnover')
        raise
    finally:
        db.execute('RELEASE compact_turnover')
