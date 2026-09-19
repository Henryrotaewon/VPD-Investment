"""Explicit PAPER-only full portfolio replacement and recoverable ledger commit."""
import copy
import hashlib
import json
import os
from pathlib import Path

from magi2.hold_policy import number

REASON = 'VPD_FULL_REBUILD'


def positive(value, label):
    result = number(value)
    if result is None or result <= 0:
        raise ValueError(f'{label} 확인 필요')
    return result


def plan_rebuild(state, snapshot, prices, cfg, now):
    """Validate everything before preparing any sale. No I/O or input mutation.

    Full replacement explicitly overrides holding/grace and same-day re-entry
    rules. An overlapping latest-TOP10 coin is sold then bought as a new lot.
    """
    if cfg.get('mode') != 'PAPER':
        raise ValueError('PAPER 계정에서만 전량 교체할 수 있습니다.')
    top_n = int(cfg['session']['top_n'])
    candidates = snapshot.get('top10', [])[:top_n]
    if top_n <= 0 or len(candidates) != top_n:
        raise ValueError(f'신규 VPD 후보 {top_n}개를 모두 확보하지 못했습니다.')
    coins, markets = set(), set()
    for row in candidates:
        coin, market = row.get('coin'), row.get('market')
        if not coin or market != 'KRW-' + coin or coin in coins or market in markets:
            raise ValueError('신규 VPD 후보 중복 또는 종목 코드 확인 필요')
        if number(row.get('VPD')) is None:
            raise ValueError(f'{coin} VPD 점수 확인 필요')
        coins.add(coin); markets.add(market)
    active = {c:p for c,p in state.get('positions', {}).items() if p.get('status','OPEN') == 'OPEN'}
    for coin, position in active.items():
        if position.get('market') != 'KRW-' + coin:
            raise ValueError(f'{coin} 보유 종목 코드 확인 필요')
        positive(position.get('qty'), f'{coin} 수량')
        positive(position.get('cost_krw'), f'{coin} 매수원가')
        markets.add(position['market'])
    quote = {market:positive(prices.get(market), market+' 현재가') for market in markets}
    cash, fee, slip = (number(x) for x in (state.get('cash_krw'), cfg.get('fee_rate',.0005), cfg.get('slippage_rate',.001)))
    if cash is None or cash < 0 or fee is None or not 0 <= fee < 1 or slip is None or slip < 0:
        raise ValueError('예수금 또는 모의 매매 비용 설정 확인 필요')
    tp = positive(cfg['exit']['take_profit_pct'],'목표 익절률')
    stop, warning = number(cfg['exit']['hard_stop_pct']), number(cfg['exit']['warning_profit_pct'])
    if stop is None or stop >= 0 or warning is None:
        raise ValueError('손절·경고 설정 확인 필요')
    asof, stamp, today = snapshot['asof'], now.isoformat(), now.date().isoformat()
    sid = 'VPD-FULL-' + hashlib.sha256(asof.encode()).hexdigest()[:20]
    next_state = copy.deepcopy(state)
    positions = next_state.setdefault('positions', {})
    realized = float(state.get('realized_pnl_krw',0)) if state.get('last_rebalance_date') == today else 0.
    lifetime = float(state.get('lifetime_realized_pnl_krw',0))
    events, sell_fees, sell_pnl = [], 0., 0.
    for coin, old in active.items():
        p = positions[coin]; px = quote[p['market']]
        gross = float(p['qty']) * px; sell_fee = gross * fee
        proceeds = gross - sell_fee; pnl = proceeds - float(p['cost_krw'])
        ret = pnl / float(p['cost_krw']) * 100
        p.update(status='CLOSED', exit_at=stamp, exit_price=px, sell_fee_krw=sell_fee,
                 net_proceeds_krw=proceeds, pnl_krw=pnl, return_pct=ret, exit_reason=REASON,
                 exit_session_id=sid)
        cash += proceeds; sell_fees += sell_fee; sell_pnl += pnl
        events.append(dict(ts=stamp,type='SELL',cohort_id=state.get('cohort_id'),session_id=sid,
                           coin=coin,market=p['market'],reason=REASON,market_price=px,
                           sell_fee_krw=round(sell_fee,2),net_proceeds_krw=round(proceeds,2),
                           pnl_krw=round(pnl,2),return_pct=round(ret,4),entry_at=old.get('entry_at'),
                           entry_session=old.get('entry_session','AM')))
    positive(cash,'재편입 가능금액')
    slot = cash / top_n
    buy_fees = 0.
    for row in candidates:
        coin, market = row['coin'], row['market']; px = quote[market]
        old = positions.get(coin)
        if old:
            next_state.setdefault('closed_positions', []).append(dict(old,coin=coin))
        fill = px * (1 + slip); notional = slot / (1 + fee); buy_fee = slot - notional
        buy_fees += buy_fee
        positions[coin] = dict(market=market,status='OPEN',entry_at=stamp,entry_session='FULL_REBUILD',
            entry_session_id=sid,first_selected_date=today,last_selected_date=today,consecutive_top10_days=1,
            signal_rank=row.get('Rank'),signal_vpd=row['VPD'],signal_price=row.get('price'),
            entry_market_price=px,entry_price=fill,buy_notional_krw=notional,buy_fee_krw=buy_fee,
            qty=notional/fill,cost_krw=slot,target_profit_pct=tp,stop_loss_pct=stop,
            warning_profit_pct=warning,last_price=px,peak_price=px,tp_warning_sent=False,sl_warning_sent=False)
        events.append(dict(ts=stamp,type='BUY',cohort_id=sid,session_id=sid,entry_session='FULL_REBUILD',
            coin=coin,market=market,budget_krw=round(slot,2),buy_notional_krw=round(notional,2),
            buy_fee_krw=round(buy_fee,2),market_price=px,fill_price=fill,target_profit_pct=tp,stop_loss_pct=stop,
            signal_rank=row.get('Rank'),signal_vpd=row['VPD']))
    summary = dict(asof=asof,mode='FULL_REBUILD',sold=list(active),bought=[r['coin'] for r in candidates],
                   repurchased=sorted(set(active)&coins),cash_krw=max(0.,cash-slot*top_n),
                   allocated_krw=cash,equal_buy_krw=slot,sell_pnl_krw=sell_pnl,
                   sell_fee_krw=sell_fees,buy_fee_krw=buy_fees,paper_only=True)
    next_state.update(cash_krw=summary['cash_krw'],realized_pnl_krw=realized+sell_pnl,
        lifetime_realized_pnl_krw=lifetime+sell_pnl,cohort_id=sid,cohort_date=today,
        last_rebalance_date=today,last_rebalance_vpd_asof=asof,last_full_rebuild_vpd_asof=asof,
        source_snapshot_asof_kst=snapshot.get('asof_kst',asof),source_snapshot_revision=None,
        strategy=cfg.get('paper_strategy','VPD_TOP10_EQUAL_WEIGHT'),cohort_policy='EXPLICIT_FULL_REBUILD',
        capital_model='ROLLING_EQUITY_EQUAL_WEIGHT',daily_equal_buy_krw=slot,daily_equal_buy_date=today,
        daily_equal_buy_source='FULL_REBUILD_NET_LIQUIDATION',last_rebalance_result=summary,
        last_full_rebuild_result=summary,last_rebalance_decisions=[
            f'{r["coin"]} · VPD {float(r["VPD"]):g} ({r.get("Rank","-")}위) · 전량 교체 신규 편입' for r in candidates],
        updated_at=stamp)
    events.append(dict(summary,ts=stamp,type='REBALANCE_FINISHED',session_id=sid))
    for i,event in enumerate(events):
        event.update(event_id=f'{sid}-{i}',paper_only=True,snapshot_asof=asof)
    # Reject non-finite computed balances/quantities before any durable write.
    json.dumps(next_state,allow_nan=False)
    return next_state, events, summary


def atomic_text(path, text):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.rebuild.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        stream.write(text); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def fingerprint(state):
    return hashlib.sha256(json.dumps(state,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def recover_rebuild(state_path, events_path):
    """Single PAPER worker completes a previously authorized durable batch."""
    state_path, events_path = Path(state_path).resolve(), Path(events_path).resolve()
    journal = state_path.with_name('paper_rebuild_pending.json')
    if not journal.exists(): return False
    transaction = json.loads(journal.read_text(encoding='utf-8'))
    if transaction.get('schema') != 'paper-rebuild-transaction-v1':
        raise RuntimeError('Invalid PAPER rebuild journal')
    after, events = transaction['state'], transaction['events']
    current = json.loads(state_path.read_text(encoding='utf-8'))
    if fingerprint(current) not in {transaction['before_hash'],fingerprint(after)}:
        raise RuntimeError('PAPER rebuild recovery conflict; refusing to overwrite newer state')
    text = events_path.read_text(encoding='utf-8') if events_path.exists() else ''
    ids = {json.loads(line).get('event_id') for line in text.splitlines() if line.strip()}
    missing = [event for event in events if event['event_id'] not in ids]
    atomic_text(state_path,json.dumps(after,ensure_ascii=False,indent=2,allow_nan=False))
    if missing:
        if text and not text.endswith('\n'): text+='\n'
        text+=''.join(json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n' for event in missing)
        atomic_text(events_path,text)
    journal.unlink()
    return True


def commit_rebuild(state_path, events_path, before, after, events):
    state_path = Path(state_path).resolve()
    journal = state_path.with_name('paper_rebuild_pending.json')
    if journal.exists(): raise RuntimeError('Pending PAPER rebuild must recover before a new request')
    if fingerprint(json.loads(state_path.read_text(encoding='utf-8'))) != fingerprint(before):
        raise RuntimeError('PAPER portfolio changed while preparing rebuild')
    if Path(events_path).exists():
        for line in Path(events_path).read_text(encoding='utf-8').splitlines():
            if line.strip(): json.loads(line)
    transaction = {'schema':'paper-rebuild-transaction-v1','before_hash':fingerprint(before),
                   'state':after,'events':events}
    atomic_text(journal,json.dumps(transaction,ensure_ascii=False,allow_nan=False))
    recover_rebuild(state_path,events_path)
