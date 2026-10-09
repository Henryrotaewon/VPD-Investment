"""Read-only, comparable PAPER results from authoritative strategy ledgers.

Wins are completed positions, never individual partial fills. Returns are NAV
returns, not averages of trade returns. A trading date starts at 09:00 KST
(00:00 UTC). Missing historical valuations are deliberately not reconstructed.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
from magi1.fast_performance import performance_state

DAY = 86_400_000
KST = ZoneInfo('Asia/Seoul')
NAMES = {'vpd': 'VPD', 'fast': 'FAST', 'indicator': '지표가속', 'bollinger': '더블볼린저·CCI'}
PAGE_SIZE = 7
BOUNDARY_TOLERANCE = 10 * 60_000


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('NONFINITE_PERFORMANCE_VALUE')
    return result


def timestamp(value):
    d = datetime.fromisoformat(str(value).replace(' KST', '+09:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return int(d.timestamp() * 1000)


def clock(stamp):
    return datetime.fromtimestamp(stamp / 1000, KST).strftime('%m/%d %H:%M')


def day_label(day):
    return datetime.fromtimestamp(day * DAY / 1000, KST).strftime('%Y-%m-%d')


@dataclass
class Mark:
    ts: int
    equity: float | None
    valid: bool = True


@dataclass
class Trade:
    ts: int
    pnl: float


@dataclass
class Results:
    key: str
    initial: float | None = None
    started: int | None = None
    trades: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    current: Mark | None = None
    wins_complete: bool = True
    notes: list = field(default_factory=list)


def readonly(path):
    # mode=ro is essential: a missing account must never be bootstrapped by a view.
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')  # State, fills and marks must belong to one snapshot.
    return db


def account_identity(state):
    active = {k: {f: p.get(f) for f in ('entry_at', 'qty', 'cost_krw')}
              for k, p in state.get('positions', {}).items()
              if p.get('status', 'OPEN') == 'OPEN'}
    payload = [state.get('initial_cash_krw'), state.get('cash_krw'), active]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def record_vpd_mark(state_path, state, prices, fee, stamp):
    """Persist one last valuation per trading day using already fetched quotes.

    Called by the existing PAPER worker, never by Telegram views. Failure is
    isolated from trading. The daily table survives cohort/rebalance changes.
    """
    path = Path(state_path).resolve().with_name('paper_performance.sqlite3')
    created = not path.exists()
    cash = number(state['cash_krw'])
    initial = number(state['initial_cash_krw'])
    equity = cash
    missing = []
    for symbol, p in state.get('positions', {}).items():
        if p.get('status', 'OPEN') != 'OPEN':
            continue
        price = prices.get(p['market'])
        if price is None or not math.isfinite(float(price)) or float(price) <= 0:
            missing.append(symbol)
        else:
            equity += number(p['qty']) * float(price) * (1 - fee)
    payload = dict(ts=stamp, equity=None if missing else equity, initial=initial,
                   cash=cash, missing=missing, identity=account_identity(state),
                   realized=state.get('lifetime_realized_pnl_krw'))
    db = sqlite3.connect(path, timeout=1)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS daily(day INTEGER PRIMARY KEY, ts INTEGER, payload TEXT)')
        with db:
            db.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET '
                       'ts=excluded.ts,payload=excluded.payload WHERE excluded.ts>=daily.ts',
                       (stamp // DAY, stamp, json.dumps(payload, allow_nan=False)))
    finally:
        db.close()
    if created:
        print('VPD performance storage ready: daily=09:00KST existing_account_preserved=true', flush=True)


def vpd_results(root, now):
    root = Path(root)
    path = root / 'paper_state.json'
    events_path = root / 'paper_events.jsonl'
    if (root / 'paper_rebuild_pending.json').exists():
        raise ValueError('REBUILD_IN_PROGRESS')
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    state = json.loads(path.read_text())
    result = Results('vpd', initial=number(state['initial_cash_krw']))
    positions = list(state.get('positions', {}).values()) + state.get('closed_positions', [])
    native_closed = [p for p in positions if p.get('status') == 'CLOSED' and p.get('exit_at')]
    closed = {}
    if events_path.exists():
        with events_path.open() as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get('type') != 'SELL':
                    continue
                ts = timestamp(event['ts'])
                if ts > now:
                    continue
                market = event.get('market') or 'KRW-'+event['coin']
                entry = event.get('entry_at')
                if not entry:
                    # Early events lack entry_at. Match only a unique native
                    # closed position at the same execution time (<=1 second).
                    matches = [p for p in native_closed if p['market'] == market and
                               abs(timestamp(p['exit_at'])-ts) <= 1000]
                    if len(matches) == 1:
                        entry = matches[0].get('entry_at')
                # Position identity must not use exit_at: the engine calls
                # now_iso() separately for state and event, a few µs apart.
                ident = (market, timestamp(entry) if entry else 'exit:'+str(ts))
                closed[ident] = Trade(ts, number(event['pnl_krw']))
    else:
        result.wins_complete = False
        result.notes.append('매매 이력 파일 없음 · 전체 승률 확인 대기')
    starts = []
    for p in positions:
        if p.get('entry_at'):
            starts.append(timestamp(p['entry_at']))
        if p.get('status') == 'CLOSED' and p.get('exit_at') and p.get('pnl_krw') is not None:
            # Native state keeps unrounded PnL; it supersedes the rounded event.
            ts = timestamp(p['exit_at'])
            if ts <= now:
                ident = (p['market'], timestamp(p['entry_at']) if p.get('entry_at') else 'exit:'+str(ts))
                closed[ident] = Trade(ts, number(p['pnl_krw']))
    after = (path.stat().st_mtime_ns, path.stat().st_size)
    if before != after or (root / 'paper_rebuild_pending.json').exists():
        raise ValueError('ACCOUNT_CHANGED_DURING_READ')
    result.trades = list(closed.values())
    lifetime = state.get('lifetime_realized_pnl_krw')
    if lifetime is not None and abs(sum(t.pnl for t in result.trades)-number(lifetime)) > max(.02, .011*len(result.trades)):
        result.wins_complete = False
        result.notes.append('저장 청산 손익과 누적 원장 불일치 · 이력 범위 확인 필요')
    # This is a history boundary only, not evidence of an initial NAV snapshot.
    result.started = min(starts + [t.ts for t in result.trades], default=None)
    marks_path = root / 'paper_performance.sqlite3'
    if marks_path.exists():
        db = readonly(marks_path)
        try:
            rows = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM daily WHERE ts<=? ORDER BY ts', (now,))]
        finally:
            db.close()
        result.marks = [Mark(r['ts'], r['equity'], r['equity'] is not None) for r in rows
                        if r['initial'] == result.initial]
        if rows and rows[-1]['identity'] == account_identity(state) and rows[-1]['initial'] == result.initial:
            r = rows[-1]
            result.current = Mark(r['ts'], r['equity'], r['equity'] is not None and now-r['ts'] <= 120_000)
    result.notes.append('과거 일별 평가는 저장분부터 제공 · 없는 날짜는 자료 부족')
    return result


def fast_trades(rows, positions):
    """Reconstruct round trips, carrying partial exits across trading dates."""
    pending = {}
    trades = []
    complete = True
    for row in rows:
        symbol = row['symbol']
        qty = number(row['quantity'])
        if qty <= 0:
            complete = False
            continue
        if row['side'] == 'BUY':
            if symbol in pending:
                complete = False
            pending[symbol] = dict(remaining=qty, quantity=qty, pnl=0., ts=row['ts'])
        elif row['side'] == 'SELL':
            p = pending.get(symbol)
            if p is None:
                complete = False
                continue
            p['remaining'] -= qty
            p['pnl'] += number(row['pnl'])
            tolerance = max(1e-8, p['quantity'] * 1e-12)
            if p['remaining'] < -tolerance:
                complete = False
            if p['remaining'] <= tolerance:
                trades.append(Trade(row['ts'], p['pnl']))
                del pending[symbol]
    if set(pending) != set(positions):
        complete = False
    return trades, complete


def fast_results(root, now):
    return public_paper_results(Path(root) / 'fast-observe' / 'paper-v1.sqlite3', 'fast', now)


def bollinger_results(root, now):
    return public_paper_results(Path(root) / 'bollinger-paper' / 'v1.sqlite3', 'bollinger', now)


def public_paper_results(path, key, now):
    db = readonly(path)
    try:
        state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        if key == 'fast':
            state = performance_state(state)
        result = Results(key, initial=number(state['policy']['initial']), started=state['started_ms'])
        rows = db.execute('SELECT * FROM fills WHERE ts>=? AND ts<=? ORDER BY id', (result.started, now))
        result.trades, result.wins_complete = fast_trades(rows, state['positions'])
        result.wins_complete &= (len(result.trades) == state['closed'] and
                                 sum(t.pnl > 0 for t in result.trades) == state['winning'])
        for row in db.execute('SELECT ts,payload FROM daily WHERE ts>=? AND ts<=? ORDER BY ts', (result.started, now)):
            p = json.loads(row['payload'])
            result.marks.append(Mark(row['ts'], number(p['equity']),
                                     not p.get('stale_marks') and not p.get('uncertain_positions')))
        equity = number(state['cash']) + sum(number(p['qty']) * number(p['mark']) for p in state['positions'].values())
        fresh = now-state['updated_ms'] <= 30_000 and all(
            0 <= now-p['mark_ms'] <= 2000 and not p['uncertain'] for p in state['positions'].values())
        result.current = Mark(state['updated_ms'], equity, fresh)
        if key == 'fast':
            result.notes.append('FAST 재개 이후 성과 · 과거 거래는 보존하고 집계에서 제외' if state['performance_scope']=='RESTART'
                                else '현재 FAST 원장 전체 · 이전 정책으로 체결한 기록도 포함')
            result.notes.append(f'성과 기준자산 {result.initial:,.0f}원 · 기준 {clock(result.started)} KST')
        else:
            o = state.get('observation', {})
            counts = o.get('states', {})
            ready = counts.get('READY', 0)
            result.notes += [
                f"신호 준비 {ready}/{o.get('symbols',0)}종목 · 수축 관찰 {o.get('watches',0)}종목",
                f"과거봉 준비 {counts.get('HISTORY_WARMUP',0)} · 새 완료봉 대기 {counts.get('LIVE_BAR_WARMUP',0)} · 봉 부족 {counts.get('INCOMPLETE_CANDLES',0)} · 재시도 {counts.get('HISTORY_RETRY',0)}",
                f"보유 {len(state['positions'])}종목 · 매수 대기 {len(state['pending'])}종목",
                '독립 모의자금 300만원 · 최대 10종목 · 매수·매도 각각 수수료 0.05% + 슬리피지 0.05%',
                '완료 5분봉 BB(20,2)·BB(60,2) + CCI(10) · 과거 신호 소급매수 없음',
                '현재 모의 운용 결과 · 과거 백테스트 결과가 아닙니다.']
        return result
    finally:
        db.close()


def indicator_results(root, now):
    from magi2.hourly_indicator import COHORT
    db = readonly(Path(root) / 'indicator_paper' / COHORT / 'ledger.sqlite3')
    try:
        state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        result = Results('indicator', initial=number(state['initial']), started=state['started_ms'])
        for row in db.execute("SELECT ts,payload FROM events WHERE kind='SELL' ORDER BY id"):
            p = json.loads(row['payload'])
            ts = p['fill_ms']  # Execution time, not delayed observation/recording time.
            if result.started <= ts <= now:
                result.trades.append(Trade(ts, number(p['pnl'])))
        for row in db.execute('SELECT ts,payload FROM marks WHERE ts>=? AND ts<=? ORDER BY ts', (result.started, now)):
            p = json.loads(row['payload'])
            result.marks.append(Mark(row['ts'], p['equity'], p['equity'] is not None))
        value = number(state['cash'])
        quotes = []
        unknown = False
        for symbol, p in state['positions'].items():
            quote = state['prices'].get(symbol)
            if not quote:
                unknown = True
                continue
            quotes.append(quote['ts'])
            value += number(p['qty']) * number(quote['price']) * (1-state['fee']) * (1-state['slip'])
        # Cash may have changed since the last mark (especially at 09:00).
        # Date this read-time valuation now; quote age controls its validity.
        fresh = not unknown and all(0 <= now-ts <= 300_000 for ts in quotes)
        result.current = Mark(now, None if unknown else value, fresh)
        result.notes.append('현재 지표가속 원장 · 초기화 이전 실험 제외')
        return result
    finally:
        db.close()


def load_results(root, key, now):
    try:
        r = {'vpd': vpd_results, 'fast': fast_results, 'indicator': indicator_results,
             'bollinger': bollinger_results}[key](root, now)
        if r.initial is None or r.initial <= 0:
            raise ValueError('INVALID_INITIAL_CAPITAL')
        if not r.wins_complete:
            r.notes.append('체결 이력 불완전 · 승률 집계 확인 대기')
        return r
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
        return Results(key, wins_complete=False, notes=['원장 준비 중 또는 갱신 중 · 다시 조회하세요.'])


def win_text(trades, complete=True):
    if not complete:
        return '— (자료 확인 중)'
    if not trades:
        return '— (청산 0건)'
    wins = sum(t.pnl > 0 for t in trades)
    losses = sum(t.pnl < 0 for t in trades)
    return f'{wins/len(trades)*100:.1f}% ({wins}승 {losses}패 {len(trades)-wins-losses}무)'


def return_text(mark, base):
    if mark is None or mark.equity is None or base is None or base <= 0:
        return '— (평가 대기)'
    return f'{(mark.equity/base-1)*100:+.2f}%' + ('' if mark.valid else ' (최근 관측)')


def daily_rows(result, now):
    marks = {}
    for m in sorted(result.marks, key=lambda m: m.ts):
        marks[m.ts // DAY] = m
    if result.current and result.current.ts // DAY == now // DAY:
        old = marks.get(now // DAY)
        if old is None or result.current.ts >= old.ts:
            marks[now // DAY] = result.current
    by_day = {}
    for t in result.trades:
        by_day.setdefault(t.ts // DAY, []).append(t)
    days = set(by_day) | set(marks) | {now // DAY}
    first = min(days)
    # Include gaps: a missing observation must not turn a multi-day move into a daily return.
    rows = []
    for day in range(now // DAY, first-1, -1):
        trades = by_day.get(day, [])
        end, previous = marks.get(day), marks.get(day-1)
        base = None
        if previous and previous.valid and previous.equity is not None and 0 < day*DAY-previous.ts <= BOUNDARY_TOLERANCE:
            base = previous.equity
        # New FAST/indicator accounts have an authoritative initial capital anchor.
        if result.key != 'vpd' and result.started is not None and result.started // DAY == day:
            base = result.initial
        end_ok = end and end.valid and end.equity is not None
        if day < now // DAY:
            end_ok = end_ok and 0 < (day+1)*DAY-end.ts <= BOUNDARY_TOLERANCE
        ret = return_text(end, base) if end_ok and base is not None else '— (평가 자료 부족)'
        rows.append((day, win_text(trades, result.wins_complete), ret))
    return rows


def results_keyboard(key=None, offset=0, total=0):
    buttons = [dict(text=NAMES[k]+' 결과', callback_data=f'performance:{k}:0') for k in NAMES]
    rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    rows.append([dict(text='FAST-DERIVATIVES / FAST-BEAR · MDD 비교',callback_data='nav:fast_models')])
    if key:
        navigation = []
        if offset:
            navigation.append(dict(text='◀ 최근', callback_data=f'performance:{key}:{max(0,offset-PAGE_SIZE)}'))
        navigation.append(dict(text='새로고침', callback_data=f'performance:{key}:{offset}'))
        if offset + PAGE_SIZE < total:
            navigation.append(dict(text='이전 날짜 ▶', callback_data=f'performance:{key}:{offset+PAGE_SIZE}'))
        rows.insert(0, navigation)
        if key == 'bollinger':
            rows.insert(1, [dict(text='📒 매매이력', callback_data='nav:bollinger_orders')])
        rows.append([dict(text='↩️ 전략검증', callback_data='nav:strategies')])
    else:
        rows.append([dict(text='전략 설명·검증 기준', callback_data='guide:validation'),
                     dict(text='새로고침', callback_data='nav:strategies')])
    rows.append([dict(text='↩️ 메인 메뉴', callback_data='nav:menu')])
    return {'inline_keyboard': rows}


def view(root, now, key=None, offset=0):
    if key is None:
        lines = ['🧭 전략검증 · 모의매매 결과', '누적 승률 / 수익률', '']
        for k, name in NAMES.items():
            r = load_results(root, k, now)
            lines.append(f'• {name}: {win_text(r.trades,r.wins_complete)} / {return_text(r.current,r.initial)}')
            if r.started is not None:
                lines.append(f'  기록 {clock(r.started)}부터 · 평가 {clock(r.current.ts) if r.current else "대기"}')
            if r.notes and r.initial is None:
                lines.append('  '+r.notes[0])
        if (Path(root)/'fast-models-v1').exists():
            from magi2.fast_models.report import summary
            lines += ['',summary(root,'derivatives',now),summary(root,'bear',now)]
        lines += ['', '전략별 시작일·운용 이력이 다릅니다.',
                  '승률: 비용 반영 후 전량 청산 기준 · 본전 포함 · 보유분 제외',
                  '수익률: 보유 평가 포함 총자산 / 전략별 성과 기준자산 − 1',
                  '각 전략을 선택하면 누적·일별 결과를 확인합니다.']
        return '\n'.join(lines), results_keyboard()
    if key not in NAMES:
        raise ValueError('UNKNOWN_PAPER_STRATEGY')
    r = load_results(root, key, now)
    rows = daily_rows(r, now)
    offset = min(max(0, offset)//PAGE_SIZE*PAGE_SIZE, (len(rows)-1)//PAGE_SIZE*PAGE_SIZE)
    lines = [f'• 누적 승률 {win_text(r.trades,r.wins_complete)} / 수익률 {return_text(r.current,r.initial)}',
             f'{NAMES[key]} 모의매매 [PAPER] · 일별 승률 / 수익률',
             '일자: 09:00~다음 날 09:00 KST', '']
    for day, wins, ret in rows[offset:offset+PAGE_SIZE]:
        suffix = ' · 진행 중' if day == now//DAY else ''
        lines.append(f'• {day_label(day)}{suffix}\n  승률 {wins} / 수익률 {ret}')
    lines += ['', f'{offset//PAGE_SIZE+1}/{max(1,math.ceil(len(rows)/PAGE_SIZE))}페이지',
              '승률=순이익 청산 / 전체 청산 · 분할매도는 완료 시 1건 · 본전 포함',
              '누적 수익률=보유 평가 포함 총자산 / 성과 기준자산 − 1',
              '일 수익률=당일 마지막 평가 / 전일 마지막 평가 − 1',
              '일말 평가는 09시 직전 10분 내 유효 관측만 사용']
    if r.current:
        lines.append('최근 평가 기준 '+clock(r.current.ts)+' KST')
    lines.extend(r.notes)
    return '\n'.join(lines), results_keyboard(key, offset, len(rows))
