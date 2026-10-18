"""Compact, exact five-minute turnover for additional FAST venues.

Each day contains 288 doubles; NaN denotes unknown, and zero denotes a
validated no-trade interval. Existing Upbit databases keep their old layout.
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


def compact(db, cutoff):
    if db not in _enabled:
        return
    grouped = defaultdict(list)
    for symbol, bucket, value in db.execute('SELECT symbol,bucket,value FROM baseline'):
        grouped[symbol, bucket // DAY * DAY].append((bucket % DAY // FIVE, value))
    for (symbol, day), rows in grouped.items():
        old = db.execute('SELECT values_blob FROM turnover_days WHERE symbol=? AND day=?', (symbol, day)).fetchone()
        values = unpack(old[0]) if old else [float('nan')] * 288
        for slot, value in rows:
            values[slot] = value
        db.execute('INSERT OR REPLACE INTO turnover_days VALUES(?,?,?)',
                   (symbol, day, zlib.compress(struct.pack('<288d', *values))))
    db.execute('DELETE FROM baseline')
    db.execute('DELETE FROM turnover_days WHERE day<?', ((cutoff - FIVE - 10 * DAY) // DAY * DAY,))
