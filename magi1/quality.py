"""Versioned, spread-aware quote observations and bounded exclusion summaries."""
from collections import defaultdict, deque
from datetime import datetime, timezone
from math import isfinite

VERSION = 'quote-v2'
HORIZONS = (10, 30, 60, 300)
FRESH_MS = 2000
GAP_MS = 2500


class QuoteCoverage:
    def __init__(self):
        self.latest = {}
        self.samples = defaultdict(lambda: deque(maxlen=650))

    def ingest(self, event):
        bid, ask = event.best_bid, event.best_ask
        if bid is None or ask is None or not all(isfinite(x) and x > 0 for x in (bid, ask)) or bid > ask:
            return False
        key = (event.venue, event.base)
        previous = self.latest.get(key)
        if previous and event.received_ts_ms < previous[0]:
            return False
        self.latest[key] = (event.received_ts_ms, bid, ask)
        return True

    def at(self, venue, asset, ts):
        value = self.latest.get((venue, asset))
        return value if value and 0 <= ts-value[0] <= FRESH_MS else None

    def tick(self, ts):
        for (venue, asset), value in self.latest.items():
            if 0 <= ts-value[0] <= FRESH_MS:
                q = self.samples[(venue, asset)]
                if not q or ts > q[-1][0]:
                    q.append((ts, value[1], value[2]))

    def covered(self, venue, asset, start, end):
        xs = [x for x in self.samples[(venue, asset)] if start <= x[0] <= end]
        return bool(xs) and xs[0][0]-start <= GAP_MS and end-xs[-1][0] <= GAP_MS and all(b[0]-a[0] <= GAP_MS for a,b in zip(xs,xs[1:]))


def outcome(row, cohort, horizon, samples):
    start = row['event_ts_ms']; end = start+horizon*1000
    xs = [(start,row['entry_bid'],row['entry_ask'])] + [x for x in samples if start < x[0] <= end]
    complete = len(xs)>1 and end-xs[-1][0] <= GAP_MS and all(b[0]-a[0] <= GAP_MS for a,b in zip(xs,xs[1:]))
    buy = row['direction']=='BUY'; entry = row['entry_ask'] if buy else row['entry_bid']
    returns = [(bid/entry-1) if buy else (entry-ask)/entry for _,bid,ask in xs]
    return {'shock_id':row['shock_id'],'cohort':cohort,'horizon_sec':horizon,'event_ts_ms':end,
            'status':'COMPLETE' if complete else 'MISSING_DATA','reason':None if complete else 'QUOTE_GAP',
            'forward_return':returns[-1] if complete else None,'mfe':max(returns) if complete else None,
            'mae':min(returns) if complete else None,'false_shock':returns[-1]<=0 if complete else None,
            'evaluation_version':VERSION,'price_basis':'entry_ask_exit_bid' if buy else 'hypothetical_sell_bid_exit_ask',
            'fees_included':False,'depth_slippage_included':False,'sample_interval_ms':1000}


def record_quality(storage, category, ts, asset='', horizon=0, count=1, **dimensions):
    # Bounded daily counters keep the denominator, including rejected observations.
    day = datetime.fromtimestamp(ts/1000,timezone.utc).strftime('%Y-%m-%d')
    with storage._lock:
        if not hasattr(storage,'quality_counters'):
            storage.quality_counters=storage.restore('quality_counts_v2', {})
        counters = storage.quality_counters
        key = '|'.join([day,category,str(asset),str(horizon)] + [f'{k}={v}' for k,v in sorted(dimensions.items())])
        counters[key] = counters.get(key,0)+count
        # Storage.flush persists the cached counters in the same commit as research rows.
        # Commit with normal ingestion flush; avoids an fsync for each observation.


def cleanup_invalid(storage, now):
    """One-time cleanup, summarized transactionally; valid outcomes and raw stay intact."""
    import json
    with storage._lock:
        if storage.restore('cleanup_quality_v2'):
            storage.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            return storage.restore('cleanup_quality_v2')
        storage.flush()
        counts = defaultdict(int)
        try:
            storage.db.execute('BEGIN')
            rows = storage.db.execute("SELECT rowid,kind,ts_ms,asset,payload FROM records WHERE kind IN ('evaluation','propagation_missing')").fetchall()
            ids = []
            for ident,kind,ts,asset,payload in rows:
                row=json.loads(payload)
                if kind=='evaluation' and row.get('status')!='MISSING_DATA':
                    continue
                category='legacy_'+kind
                record_quality(storage,category,ts,asset or '',row.get('horizon_sec',0),cohort=row.get('cohort',''),version='legacy')
                counts[kind]+=1; ids.append((ident,))
            storage.db.executemany('DELETE FROM records WHERE rowid=?',ids)
            result={'event_ts_ms':now,'deleted':dict(counts),'raw_deleted':0,'valid_evaluations_deleted':0}
            storage.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)',('quality_counts_v2',json.dumps(getattr(storage,'quality_counters',{}))))
            storage.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?)',('cleanup_quality_v2',json.dumps(result)))
            storage.db.commit()
        except Exception:
            storage.db.rollback()
            storage.quality_counters=storage.restore('quality_counts_v2',{})
            raise
        storage.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        storage.db.execute('VACUUM')
        storage.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return result


def quality_summary(storage):
    totals=defaultdict(int)
    with storage._lock:
        for key,value in getattr(storage,'quality_counters',{}).items():
            parts=key.split('|')
            if len(parts)>1:totals[parts[1]]+=value
    return dict(totals)
