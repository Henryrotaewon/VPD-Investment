"""Read-only FAST evidence. Features annotate signals; they never authorize buys."""
import json
from statistics import median
from magi1.fast_performance import performance_state


def acceleration(bars, end, book):
    """Compare completed adjacent 10s windows only; missing prices stay unknown."""
    rows = {b['t']: b for b in bars if b['t'] < end}
    current, prior, older = (rows.get(end-k*10000, {}) for k in (1, 2, 3))
    def change(a, b):
        return (a/b-1)*10000 if a and b else None
    r10 = change(current.get('c'), prior.get('c'))
    previous = change(prior.get('c'), older.get('c'))
    v, pv = current.get('value'), prior.get('value')
    share = lambda b: b.get('buy', 0)/b['value'] if b.get('value') else None
    buy, old_buy = share(current), share(prior)
    recent = [rows.get(end-k*10000, {}) for k in range(1, 7)]
    lows = [b.get('l') for b in recent]
    return dict(
        return_10s_bps=r10,
        price_acceleration_bps=r10-previous if r10 is not None and previous is not None else None,
        turnover_change_ratio=v/pv if v is not None and pv else None,
        buy_share_change=buy-old_buy if buy is not None and old_buy is not None else None,
        ofi_change=current.get('ofi', 0)-prior.get('ofi', 0) if current and prior else None,
        rise_from_60s_low_bps=change(current.get('c'), min(lows)) if all(lows) else None,
        spread_bps=(book['ap']/book['bp']-1)*10000 if book and book.get('bp') else None,
        diagnostic_version='fast-review-v1')


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return dict(n=0, median=None, p90=None)
    return dict(n=len(values), median=median(values), p90=values[min(len(values)-1, int(len(values)*.9))])


def ledger_review(paper):
    """Aggregate complete round trips, including partial fills, without rewriting history."""
    db = paper.db
    performance = performance_state(paper.s)
    cutoff = performance['started_ms']
    cursor = db.execute('''
        WITH buys AS (
          SELECT *, LEAD(id) OVER (PARTITION BY symbol ORDER BY id) next_id
          FROM fills WHERE side='BUY' AND ts>=?
        )
        SELECT b.id,b.ts,b.symbol,b.quantity,b.cash,b.fee,b.decision_ms,
               COALESCE(SUM(s.quantity),0),COALESCE(SUM(s.pnl),0),MAX(s.ts),
               COALESCE(SUM(s.fee),0),GROUP_CONCAT(DISTINCT s.reason)
        FROM buys b LEFT JOIN fills s ON s.symbol=b.symbol AND s.side='SELL'
          AND s.id>b.id AND (b.next_id IS NULL OR s.id<b.next_id)
        GROUP BY b.id ORDER BY b.id
    ''', (cutoff,))
    completed = []; by_reason = {}; by_symbol = {}; latency = []
    for ident, ts, symbol, qty, cash, fee, decision, sold, pnl, exit_ts, sell_fee, reasons in cursor:
        latency.append(ts-decision)
        if qty-sold > 1e-8:
            continue
        reason = reasons or 'UNKNOWN'
        completed.append(dict(pnl=pnl, cost=abs(cash), hold_ms=exit_ts-ts, fee=fee+sell_fee))
        for target, key in ((by_reason, reason), (by_symbol, symbol)):
            row = target.setdefault(key, dict(trades=0, wins=0, pnl=0.))
            row['trades'] += 1; row['wins'] += pnl > 0; row['pnl'] += pnl
    gross_profit = sum(max(0, r['pnl']) for r in completed)
    gross_loss = -sum(min(0, r['pnl']) for r in completed)
    def events(kind):
        records = [json.loads(x[0]) for x in db.execute('SELECT payload FROM events WHERE kind=? AND ts>=?', (kind, cutoff))]
        return [r for r in records if r.get('entry_ms', cutoff) >= cutoff]
    exits = events('EXIT_DIAGNOSTIC')
    entries = events('ENTRY_DIAGNOSTIC')
    continuous = [r for r in exits if r['continuous']]
    post = events('POST_EXIT_DIAGNOSTIC')
    post_stats = {str(h): dict(observed=distribution(r.get('difference_pct') for r in post if r['horizon_ms']==h),
        missing=sum(r['horizon_ms']==h and r['status']=='MISSING' for r in post)) for h in (60000,180000,300000)}
    cohorts = {}
    for r in exits:
        a = r.get('price_acceleration_bps')
        key = ('GAP_' if not r['continuous'] else 'NORMAL_') + ('UNKNOWN' if a is None else 'POSITIVE' if a > 0 else 'NONPOSITIVE')
        row = cohorts.setdefault(key, dict(trades=0, wins=0, pnl=0.))
        row['trades'] += 1; row['wins'] += r['pnl'] > 0; row['pnl'] += r['pnl']
    return dict(mode='FAST_LEDGER_REVIEW', policy=paper.policy['version'],
        performance_scope=performance['performance_scope'], started_ms=cutoff,
        initial=performance['policy']['initial'],
        completed=len(completed), wins=sum(r['pnl'] > 0 for r in completed),
        net_pnl=sum(r['pnl'] for r in completed), explicit_fees=sum(r['fee'] for r in completed),
        profit_factor=gross_profit/gross_loss if gross_loss else None,
        holding_ms=distribution(r['hold_ms'] for r in completed), fill_delay_ms=distribution(latency),
        exit_reasons=by_reason, symbols=by_symbol,
        initial_stop_distance_bps=distribution(r.get('stop_distance_bps') for r in entries),
        immediate_exit_net_pct=distribution(r.get('immediate_exit_net_pct') for r in entries),
        confirmation_rise_bps=distribution(r.get('confirmation_rise_bps') for r in entries),
        max_favorable_net_pct=distribution(r['best_exit_net_pct'] for r in continuous),
        max_adverse_net_pct=distribution(r['worst_exit_net_pct'] for r in continuous),
        giveback_pct=distribution(r['best_exit_net_pct']-r['net_return_pct'] for r in continuous if r['best_exit_net_pct'] is not None),
        exit_delay_ms=distribution(r['exit_delay_ms'] for r in exits),
        post_exit_quote_difference_pct=post_stats,
        diagnostics_closed=len(exits), acceleration_cohorts=cohorts,
        caveat='Acceleration cohorts are observational, not a causal or out-of-sample test; old MFE/MAE is unavailable.')
