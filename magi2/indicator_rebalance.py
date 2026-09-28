"""Confirmed, resumable manual portfolio actions for the hourly PAPER ledger."""
import hashlib
import json
from magi2.indicator_protection import effective_stop

HOUR = 3600000
DAY = 86400000
STEP = 300000
MIN_CANDIDATES = 1


class RebalanceUnavailable(ValueError):
    pass


def restore_confirmed(ledger, stamp):
    """Derive the new cache from existing evidence, without replaying trades."""
    if 'confirmed' in ledger.s:
        return
    recent = {}
    pool = {}
    rows = ledger.db.execute("SELECT symbol,payload FROM events WHERE kind='OBSERVATION' AND ts>=? ORDER BY id",
                             (stamp-2*HOUR,)).fetchall()
    for symbol, raw in rows:
        point = json.loads(raw)
        old = recent.get(symbol)
        recent[symbol] = point
        if (old and old['boundary']+HOUR == point['boundary'] and old['day'] == point['day'] and
                old['qualifies'] and point['qualifies'] and ledger.s['previous'].get(symbol) == point):
            pool[symbol] = point
        else:
            pool.pop(symbol, None)
    ledger.s['confirmed'] = pool
    ledger.save()


def scan_ready(s, stamp):
    scan = s['scan']
    return (scan.get('status') == 'MONITORING' and not scan.get('bootstrap') and
            scan.get('boundary') == stamp // HOUR * HOUR)


def candidates(ledger, stamp):
    """Unique, still-confirmed markets in the latest completed hourly scan."""
    s = ledger.s
    if not scan_ready(s, stamp):
        return {}
    scan = s['scan']
    boundary = stamp // HOUR * HOUR
    result = {}
    for symbol, point in sorted(s.get('confirmed', {}).items()):
        current = s['previous'].get(symbol, {})
        pos = s['positions'].get(symbol, {})
        stop = max(point['low'], effective_stop(pos,s) if pos else 0)
        if (symbol in scan.get('names', {}) and point['boundary'] == boundary and
                current == point and point['day'] == stamp // DAY * DAY and
                point['qualifies'] and not point['trend_exit'] and point['close'] >= stop and
                boundary > s['last_exit'].get(symbol, 0)):
            result[symbol] = dict(point, initial_stop=stop)
    return result


def plan(ledger, mode, stamp):
    with ledger.lock:
        s = ledger.s
        if mode not in ('rebuild', 'refill'):
            raise RebalanceUnavailable('지원하지 않는 실행입니다.')
        if not s['enabled']:
            raise RebalanceUnavailable('포착·매매가 정지 상태입니다. 시작 후 새 관측을 기다려 주세요.')
        if s.get('rebalance', {}).get('phase') in ('SELLING', 'BUYING'):
            raise RebalanceUnavailable('앞선 전량교체·리필이 진행 중입니다.')
        if s['pending']:
            raise RebalanceUnavailable('기존 매수·매도 대기가 있습니다. 체결 완료 후 다시 선택하세요.')
        vacant = s['slots'] if mode == 'rebuild' else s['slots'] - len(s['positions'])
        if vacant <= 0:
            raise RebalanceUnavailable('채울 빈자리가 없습니다. 기존 보유를 유지합니다.')
        if not scan_ready(s, stamp):
            raise RebalanceUnavailable('현재 시간의 정시 스캔이 완료되지 않았습니다. 스캔 완료 후 다시 확인하세요.')
        pool = candidates(ledger, stamp)
        if len(pool) < MIN_CANDIDATES:
            raise RebalanceUnavailable('최신 정시 스캔 완료 · 유효 포착 0종목. 기존 보유·현금을 유지합니다. 다음 유효 포착 후 다시 확인하세요.')
        targets = {k:v for k,v in pool.items() if mode == 'rebuild' or k not in s['positions']}
        targets = dict(list(targets.items())[:vacant])
        if not targets:
            raise RebalanceUnavailable(f'최신 유효 포착 {len(pool)}종목은 모두 보유 중입니다. 새로 편입할 종목이 없어 기존 보유·현금을 유지합니다.')
        if mode == 'refill' and s['cash'] / vacant < 5000:
            raise RebalanceUnavailable('빈 슬롯당 가용현금이 최소 모의매수금액 5,000원 미만입니다.')
        result = dict(mode=mode, boundary=stamp//HOUR*HOUR, candidates=len(pool), targets=targets,
                      sells=list(s['positions']) if mode == 'rebuild' else [], vacant=vacant)
        basis = dict(result, positions=s['positions'], cash=s['cash'], generation=s['generation'])
        result['fingerprint'] = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:24]
        return result


def request(ledger, mode, stamp, fingerprint):
    with ledger.lock, ledger.db:
        prepared = plan(ledger, mode, stamp)
        if prepared['fingerprint'] != fingerprint:
            raise RebalanceUnavailable('확인 중 포착·보유 상태가 변경되었습니다. 새 미리보기를 확인하세요.')
        s = ledger.s
        if fingerprint in s.get('manual_requests', []):
            raise RebalanceUnavailable('이미 처리한 요청입니다.')
        s['manual_requests'] = (s.get('manual_requests', []) + [fingerprint])[-100:]
        s['rebalance'] = dict(prepared, phase='SELLING', requested_ms=stamp, bought=[], canceled=[])
        ledger.event(stamp, 'REBALANCE_REQUEST', '', s['rebalance'])
        for symbol in prepared['sells']:
            ledger.schedule_exit(symbol, stamp, 'MANUAL_INDICATOR_REBUILD', prepared['boundary'])
        ledger.save()
        advance(ledger, stamp)
        return dict(s['rebalance'])


def _finish(ledger, stamp, phase, reason):
    op = ledger.s['rebalance']
    if phase == 'CANCELED':
        op['canceled'] = sorted(set(op['canceled']) | (set(op['targets']) - set(op['bought'])))
    op.update(phase=phase, finished_ms=stamp, reason=reason)
    ledger.event(stamp, 'REBALANCE_FINISH', '', op)
    ledger.save()


def advance(ledger, stamp):
    """Sell first, then reserve only settled cash. State survives restarts."""
    with ledger.lock, ledger.db:
        s = ledger.s
        op = s.get('rebalance', {})
        if op.get('phase') not in ('SELLING', 'BUYING'):
            return
        if not s['enabled']:
            _finish(ledger, stamp, 'CANCELED', 'MANUAL_CLEAR')
            return
        if op['phase'] == 'BUYING':
            if not any(o.get('manual_request') == op['fingerprint'] for o in s['pending'].values()):
                _finish(ledger, stamp, 'DONE', 'COMPLETED' if len(op['bought']) == op['vacant'] else 'PARTIAL_CASH_RETAINED')
            return
        if any(symbol in s['positions'] for symbol in op['sells']):
            return
        # A new hour/day invalidates the approved selection; never chase it later.
        if stamp//HOUR*HOUR != op['boundary']:
            _finish(ledger, stamp, 'CANCELED', 'SIGNAL_EXPIRED_CASH_RETAINED')
            return
        scan = s['scan']
        if scan.get('status') != 'MONITORING' or scan.get('boundary') != op['boundary']:
            _finish(ledger, stamp, 'CANCELED', 'SCAN_UNAVAILABLE_CASH_RETAINED')
            return
        vacant = s['slots'] - len(s['positions'])
        budget = s['cash'] / vacant if vacant else 0
        if budget < 5000:
            _finish(ledger, stamp, 'CANCELED', 'INSUFFICIENT_CASH')
            return
        for symbol, point in op['targets'].items():
            current = s['previous'].get(symbol, {})
            if (symbol in s['positions'] or symbol in s['pending'] or
                    current.get('boundary') != op['boundary'] or not current.get('qualifies') or
                    current.get('trend_exit') or current.get('close', 0) < point['initial_stop']):
                op['canceled'].append(symbol)
                continue
            s['pending'][symbol] = dict(side='BUY', reason='MANUAL_INDICATOR_'+op['mode'].upper(),
                decision_ms=stamp, signal_ms=op['boundary'], earliest=(stamp//STEP+1)*STEP,
                expires_ms=op['boundary']+HOUR, budget=budget, initial_stop=point['initial_stop'],
                generation=s['generation'], manual_request=op['fingerprint'])
            # Manual action may reuse a slot-skipped signal; ordinary entries remain one/day.
            s['used_day'][symbol] = point['day']
        op['phase'] = 'BUYING'
        ledger.save()


def cancel_entry(ledger, symbol, order, stamp, reason):
    """Caller holds the ledger lock; no loss of quantity/accounting precision."""
    del ledger.s['pending'][symbol]
    op = ledger.s.get('rebalance', {})
    if order.get('manual_request') and op.get('fingerprint') == order['manual_request']:
        op['canceled'].append(symbol)
    ledger.event(stamp, 'CANCEL', symbol, dict(order, reason=reason))
    ledger.save()


def preview_text(prepared):
    mode = prepared['mode']
    title = '전량교체' if mode == 'rebuild' else '종목 리필'
    action = (f'기존 {len(prepared["sells"])}종목 전량 모의매도 후 최대 {len(prepared["targets"])}종목 균등 재매수' if mode == 'rebuild'
              else f'기존 보유 유지 · 빈자리 최대 {len(prepared["targets"])}종목 매수')
    costs = ('기존 보유도 재편입될 수 있으며 편도 수수료·슬리피지 각 0.05% 반영' if mode == 'rebuild'
             else '보유 수량·보호선 유지 · 신규 매수 편도 수수료·슬리피지 각 0.05% 반영')
    return (f'지표가속 {title} [PAPER]\n최신 유효 포착 {prepared["candidates"]}종목\n{action}\n'
            f'배분 대상 {prepared["vacant"]}슬롯 중 {len(prepared["targets"])}슬롯 편입 · 나머지 {prepared["vacant"]-len(prepared["targets"])}슬롯 몫은 현금 유지\n'
            '편입 예정: '+', '.join(prepared['targets'])+'\n'
            '동시 후보는 기존 방식인 마켓 코드순 · 매도 후 가용현금/빈 슬롯 균등배분\n'
            '확정 이후 실제 다음 5분봉 시가로 모의체결 · 최초원금·누적손익 보존\n'
            f'{costs}\n'
            '시간 경과·보호선 이탈로 매수하지 못한 금액은 현금 유지\n'
            '60초 안에 확인 또는 취소를 선택하세요.')
