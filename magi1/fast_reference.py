"""Separate live decision candles from historical same-time turnover references.

No fallback to a different time bucket, shortened history, or invented candles.
The rolling retention floor includes the oldest reference of the last CLOSED bar.
"""
DAY = 86_400_000
FIVE = 300_000


def bounds(cutoff):
    end = int(cutoff) // FIVE * FIVE
    return end - FIVE - 10 * DAY, end


def reference_state(db, symbol, cutoff):
    _, end = bounds(cutoff)
    full = end - FIVE
    # Detailed current input and historical comparator have distinct read paths.
    current = db.execute('SELECT value,source FROM recent5m WHERE symbol=? AND bucket=?',
                         (symbol, full)).fetchone()
    keys = [full - k * DAY for k in range(1, 11)]
    past = dict(db.execute('SELECT bucket,value FROM baseline WHERE symbol=? AND bucket IN ('
                          + ','.join('?' * 10) + ')', (symbol, *keys)))
    reasons = []
    if current is None:
        reasons.append('CURRENT_BAR_MISSING')
    if len(past) != 10:
        reasons.append('HISTORY_INCOMPLETE')
    elif sum(past.values()) <= 0:
        reasons.append('HISTORY_ZERO_VALUE')
    ratio = current[0] / (sum(past.values()) / 10) if not reasons else None
    return dict(ready=not reasons, reasons=reasons, five_minute_start=full,
                current_value=current[0] if current else None,
                current_source=current[1] if current else None,
                baseline_days=len(past), relative_value=ratio,
                oldest_reference=keys[-1])
