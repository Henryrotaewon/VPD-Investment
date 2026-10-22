"""Explicit, one-time indicator PAPER reset, before its workers start.

Only indicator records in its ledger and shared audit caches are cleared. The
request receipt is committed with the fresh state so restarts cannot repeat it.
"""
import json
import sqlite3

RESET_ID='indicator-cleanstart-20261022'


def clean_audit_cache(ledger,log):
    """Retryable cleanup before workers start; preserve other strategies."""
    from magi2.capture_audit import atomic_json
    receipt=ledger.s['reset_receipt']
    if receipt.get('audit_cleaned'):return
    directory=ledger.root/'capture-audit';path=directory/'evidence.sqlite3'
    evidence=0;reports=0
    if path.exists():
        db=sqlite3.connect(path,timeout=5)
        try:
            with db:
                evidence=db.execute("DELETE FROM evidence WHERE source='indicator'").rowcount
                db.execute("DELETE FROM meta WHERE key='indicator'")
        finally:db.close()
    for path in sorted(set(directory.glob('v[0-9]*-[0-9]*.json'))|{directory/'latest.json'}):
        if not path.exists():continue
        report=json.loads(path.read_text())
        changed=False
        for row in report.get('rows',[]):
            if 'indicator' in row.get('strategies',{}):
                row['strategies']['indicator']=dict(status='초기화',
                    detail='이전 지표가속 실험 초기화 · 새 실험 집계 대기',reset_ms=receipt['reset_ms'])
                changed=True
        if 'indicator' in report.get('evidence_errors',{}):
            del report['evidence_errors']['indicator'];changed=True
        if changed:atomic_json(path,report);reports+=1
    # If a process dies before this commit, repeating only cache cleanup is safe:
    # no indicator workers have been allowed to start, and capital is not reset.
    receipt['audit_cleaned']=True;ledger.save()
    log('indicator_reset_audit_cleaned '+json.dumps(dict(evidence=evidence,reports=reports,
        other_strategy_records_preserved=True)))


def diagnose_pending(ledger,market,stamp,log):
    from magi2.hourly_indicator import iso,normalized
    for symbol,order in ledger.pending().items():
        evidence=dict(market=symbol,side=order['side'],earliest_ms=order['earliest'],
                      decision_ms=order['decision_ms'],observed_ms=stamp,
                      source='upbit/candles/minutes/5')
        try:
            # Reproduce the old request and guard without executing any order.
            rows=normalized(market.get('candles/minutes/5',
                {'market':symbol,'to':iso(stamp),'count':200}))
            evidence.update(rows=len(rows),oldest_ms=rows[0][0] if rows else None,
                            newest_ms=rows[-1][0] if rows else None)
            evidence['legacy_result']=('FILL_WINDOW_MISSING' if rows and len(rows)>=200
                and rows[0][0]>order['earliest'] else 'WINDOW_COVERED' if rows else 'NO_CANDLES')
        except Exception as exc:
            evidence.update(legacy_result=type(exc).__name__,reason=str(exc)[:160])
        log('indicator_reset_diagnosis '+json.dumps(evidence))


def reset_once(ledger,request_id,stamp,log,market=None):
    from magi2.hourly_indicator import initial_state,PublicCandles
    if request_id!=RESET_ID:raise ValueError('INVALID_INDICATOR_RESET_REQUEST')
    if ledger.s.get('reset_receipt',{}).get('id')==request_id:
        clean_audit_cache(ledger,log)
        return False
    # Validate PAPER configuration before diagnosis or any mutation.
    fresh=initial_state(stamp)
    with ledger.lock:
        before=dict(positions=list(ledger.s['positions']),pending=list(ledger.s['pending']),
                    scan=ledger.s.get('scan',{}).get('errors',{}),
                    events=ledger.db.execute('SELECT count(*) FROM events').fetchone()[0],
                    marks=ledger.db.execute('SELECT count(*) FROM marks').fetchone()[0])
        log('indicator_reset_before '+json.dumps(before))
        diagnose_pending(ledger,market if market is not None else PublicCandles(),stamp,log)
        fresh.update(generation=ledger.s['generation']+1,reset_done=True,confirmed={},
                     reset_receipt=dict(id=request_id,reset_ms=stamp))
        # State, event history and all daily performance source rows change in
        # one transaction. Failure leaves the old state and receipt unchanged.
        with ledger.db:
            ledger.db.execute('DELETE FROM events')
            ledger.db.execute('DELETE FROM marks')
            ledger.db.execute('UPDATE state SET payload=? WHERE id=1',
                              (json.dumps(fresh,allow_nan=False),))
        ledger.s=fresh
        clean_audit_cache(ledger,log)
        log('indicator_reset_completed '+json.dumps(dict(reset_id=request_id,
            started_ms=stamp,initial=fresh['initial'],cash=fresh['cash'],realized=0,
            positions=0,pending=0,events=0,marks=0,enabled=True,vpd_untouched=True,
            live_orders=False)))
    return True
