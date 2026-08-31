import json, os, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / 'magi2' / 'config.json').read_text(encoding='utf-8'))
STATE_PATH = ROOT / 'magi2' / 'state' / 'paper_state.json'
EVENTS_PATH = ROOT / 'magi2' / 'state' / 'paper_events.jsonl'
VPD_PATH = ROOT / 'data' / 'vpd_latest.json'
KST = ZoneInfo('Asia/Seoul')

# MAGI2 BASE policy
# AM: fresh TOP10 -> KEEP overlap / VPD_EXIT drops / BUY new names.
# PM: fresh evening TOP10 -> never sell existing names; refill only vacant capital slots.
# PM refill positions become normal OPEN positions and face the next AM TOP10 review immediately.
# No minimum 24h holding rule. Every BUY keeps its own entry_at/entry_session.
# TP/SL is always independent.


def now_dt():
    return datetime.now(KST)


def now_iso():
    return now_dt().isoformat()


def parse_hhmm(s):
    return datetime.strptime(s, '%H:%M').time()


def telegram(text):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': text},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f'Telegram error: {e}')


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    st['updated_at'] = now_iso()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')


def load_state():
    if not STATE_PATH.exists():
        raise RuntimeError(
            'MAGI2 paper_state.json is missing. Refusing to initialize a new 3,000,000 KRW account automatically.'
        )
    return json.loads(STATE_PATH.read_text(encoding='utf-8'))


def log_event(evt):
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(evt, ensure_ascii=False) + '\n')


def get_prices(markets):
    if not markets:
        return {}
    out = {}
    for i in range(0, len(markets), 100):
        chunk = markets[i:i + 100]
        try:
            r = requests.get(
                'https://api.upbit.com/v1/ticker',
                params={'markets': ','.join(chunk)},
                timeout=15,
            )
            r.raise_for_status()
            for x in r.json():
                out[x['market']] = float(x['trade_price'])
        except Exception as e:
            print(f'price error: {e}')
    return out


def position_return(p, px):
    fee_rate = float(CFG.get('fee_rate', 0.0005))
    net_liq = float(p['qty']) * float(px) * (1.0 - fee_rate)
    return (net_liq / float(p['cost_krw']) - 1.0) * 100.0


def close_position(st, coin, p, px, reason):
    if p.get('status', 'OPEN') != 'OPEN':
        return
    fee_rate = float(CFG.get('fee_rate', 0.0005))
    gross = float(p['qty']) * float(px)
    sell_fee = gross * fee_rate
    proceeds = gross - sell_fee
    pnl = proceeds - float(p['cost_krw'])
    ret = (proceeds / float(p['cost_krw']) - 1.0) * 100.0
    p.update({
        'status': 'CLOSED',
        'exit_at': now_iso(),
        'exit_price': float(px),
        'sell_fee_krw': sell_fee,
        'net_proceeds_krw': proceeds,
        'pnl_krw': pnl,
        'return_pct': ret,
        'exit_reason': reason,
    })
    st['cash_krw'] = float(st.get('cash_krw', 0)) + proceeds
    st['realized_pnl_krw'] = float(st.get('realized_pnl_krw', 0)) + pnl
    st['lifetime_realized_pnl_krw'] = float(st.get('lifetime_realized_pnl_krw', 0)) + pnl
    log_event({
        'ts': now_iso(), 'type': 'SELL', 'cohort_id': st.get('cohort_id'),
        'coin': coin, 'market': p['market'], 'reason': reason,
        'market_price': px, 'sell_fee_krw': round(sell_fee, 2),
        'net_proceeds_krw': round(proceeds, 2), 'pnl_krw': round(pnl, 2),
        'return_pct': round(ret, 4), 'entry_at': p.get('entry_at'),
        'entry_session': p.get('entry_session', 'AM'), 'paper_only': True,
    })
    telegram(
        f'🔴 MAGI2 PAPER 청산\n{coin} / {reason}\n'
        f'순수익률 {ret:+.2f}% / 손익 {pnl:+,.0f}원\nPAPER ONLY'
    )


def portfolio_status(st, prices, title='📊 MAGI2 PAPER 현황'):
    cash = float(st.get('cash_krw', 0))
    open_value = 0.0
    rows = []
    for coin, p in st.get('positions', {}).items():
        if p.get('status', 'OPEN') != 'OPEN':
            continue
        px = prices.get(p['market'], float(p.get('last_price', p['entry_price'])))
        fee_rate = float(CFG.get('fee_rate', 0.0005))
        value = float(p['qty']) * px * (1.0 - fee_rate)
        open_value += value
        rows.append((coin, position_return(p, px), p.get('entry_session', 'AM')))
    equity = cash + open_value
    base = float(st.get('initial_cash_krw', CFG['initial_cash_krw']))
    ret = (equity / base - 1.0) * 100.0 if base else 0.0
    lines = [
        title,
        f"Cohort {st.get('cohort_id', '-')}",
        f'최초원금 {base:,.0f}원',
        f'평가 {equity:,.0f}원 / 누적 {ret:+.2f}% ({equity-base:+,.0f}원)',
        f'예수금 {cash:,.0f}원',
    ]
    for coin, r, sess in sorted(rows, key=lambda x: x[1], reverse=True):
        tag = ' PM' if sess == 'PM_REFILL' else ''
        lines.append(f'{coin}{tag} {r:+.2f}%')
    lines.append(f"금일 실현손익 {float(st.get('realized_pnl_krw', 0)):+,.0f}원")
    lines.append('수익률=슬리피지+매수/매도 수수료 반영 · PAPER ONLY')
    return '\n'.join(lines)


def migrate_buy_fee(st):
    changed = False
    fee_rate = float(CFG.get('fee_rate', 0.0005))
    for p in st.get('positions', {}).values():
        if 'buy_fee_krw' not in p:
            old_cost = float(p.get('cost_krw', CFG.get('position_krw', 300000)))
            buy_notional = old_cost / (1.0 + fee_rate)
            p['buy_notional_krw'] = buy_notional
            p['buy_fee_krw'] = old_cost - buy_notional
            old_fill = float(p.get('entry_price', 0))
            p['qty'] = buy_notional / old_fill if old_fill else float(p.get('qty', 0))
            p['cost_krw'] = old_cost
            changed = True
        if 'entry_session' not in p:
            p['entry_session'] = 'AM'
            changed = True
    if changed:
        save_state(st)


def load_snapshot_window(start_key, end_key, default_start, default_end):
    if not VPD_PATH.exists():
        return None
    snap = json.loads(VPD_PATH.read_text(encoding='utf-8'))
    raw = snap.get('asof')
    if not raw:
        return None
    asof = datetime.fromisoformat(raw).astimezone(KST)
    session = CFG.get('session', {})
    start = parse_hhmm(session.get(start_key, default_start))
    end = parse_hhmm(session.get(end_key, default_end))
    if asof.date() != now_dt().date() or not (start <= asof.time() <= end):
        return None
    return snap, asof


def portfolio_equity(st, prices=None):
    prices = prices or {}
    cash = float(st.get('cash_krw', 0))
    value = 0.0
    fee_rate = float(CFG.get('fee_rate', 0.0005))
    for p in st.get('positions', {}).values():
        if p.get('status', 'OPEN') != 'OPEN':
            continue
        px = prices.get(
            p['market'],
            float(p.get('last_price', p.get('entry_market_price', p['entry_price']))),
        )
        value += float(p['qty']) * px * (1.0 - fee_rate)
    return cash + value


def archive_closed_slot(st, coin):
    old = st.get('positions', {}).get(coin)
    if old and old.get('status') == 'CLOSED':
        st.setdefault('closed_positions', []).append(dict(old, coin=coin))


def buy_position(st, row, live_px, budget, session_name, session_id, selected_date):
    if budget <= 0 or live_px is None or float(st.get('cash_krw', 0)) + 1e-9 < budget:
        return False
    slippage = float(CFG.get('slippage_rate', 0.001))
    fee_rate = float(CFG.get('fee_rate', 0.0005))
    tp = float(CFG['exit']['take_profit_pct'])
    sl = float(CFG['exit']['hard_stop_pct'])
    warning = float(CFG['exit']['warning_profit_pct'])
    coin, market = row['coin'], row['market']
    fill = live_px * (1.0 + slippage)
    buy_notional = budget / (1.0 + fee_rate)
    buy_fee = budget - buy_notional
    qty = buy_notional / fill
    archive_closed_slot(st, coin)
    st['positions'][coin] = {
        'market': market, 'status': 'OPEN', 'entry_at': now_iso(),
        'entry_session': session_name, 'entry_session_id': session_id,
        'first_selected_date': selected_date, 'last_selected_date': selected_date,
        'consecutive_top10_days': 1, 'signal_rank': row.get('Rank'),
        'signal_vpd': row.get('VPD'), 'signal_price': row.get('price'),
        'entry_market_price': live_px, 'entry_price': fill,
        'buy_notional_krw': buy_notional, 'buy_fee_krw': buy_fee,
        'qty': qty, 'cost_krw': budget, 'target_profit_pct': tp,
        'stop_loss_pct': sl, 'warning_profit_pct': warning,
        'last_price': live_px, 'peak_price': live_px,
    }
    st['cash_krw'] -= budget
    log_event({
        'ts': now_iso(), 'type': 'BUY', 'cohort_id': st.get('cohort_id'),
        'session_id': session_id, 'entry_session': session_name,
        'coin': coin, 'market': market, 'budget_krw': round(budget, 2),
        'buy_notional_krw': round(buy_notional, 2), 'buy_fee_krw': round(buy_fee, 2),
        'market_price': live_px, 'fill_price': fill, 'target_profit_pct': tp,
        'stop_loss_pct': sl, 'signal_rank': row.get('Rank'),
        'signal_vpd': row.get('VPD'), 'paper_only': True,
    })
    return True


def maybe_rebalance_morning(st):
    loaded = load_snapshot_window('entry_after_kst', 'entry_window_end_kst', '07:20', '08:00')
    if loaded is None:
        return False
    snap, asof = loaded
    today = asof.date().isoformat()
    if st.get('last_rebalance_date') == today:
        return False
    candidates = snap.get('top10', [])[:int(CFG.get('session', {}).get('top_n', 10))]
    if not candidates:
        return False

    top_by_coin = {row['coin']: row for row in candidates}
    top_coins = set(top_by_coin)
    active = {c: p for c, p in st.get('positions', {}).items() if p.get('status', 'OPEN') == 'OPEN'}
    all_markets = list({p['market'] for p in active.values()} | {row['market'] for row in candidates})
    prices = get_prices(all_markets)
    equity_before = portfolio_equity(st, prices)
    kept, exited, bought = [], [], []

    for coin, p in list(active.items()):
        if coin in top_coins:
            p['last_selected_date'] = today
            p['consecutive_top10_days'] = int(p.get('consecutive_top10_days', 1)) + 1
            p['signal_rank'] = top_by_coin[coin].get('Rank')
            p['signal_vpd'] = top_by_coin[coin].get('VPD')
            kept.append(coin)
            log_event({
                'ts': now_iso(), 'type': 'KEEP', 'cohort_id': f'{today}-AM-001',
                'coin': coin, 'market': p['market'], 'signal_rank': p.get('signal_rank'),
                'signal_vpd': p.get('signal_vpd'),
                'consecutive_top10_days': p['consecutive_top10_days'],
                'entry_at': p.get('entry_at'), 'entry_session': p.get('entry_session', 'AM'),
                'paper_only': True,
            })
        else:
            px = prices.get(p['market'])
            if px is not None:
                close_position(st, coin, p, px, 'VPD_EXIT')
                exited.append(coin)

    open_after = {c: p for c, p in st.get('positions', {}).items() if p.get('status', 'OPEN') == 'OPEN'}
    new_rows = [row for row in candidates if row['coin'] not in open_after]
    slot = float(CFG.get('position_krw', 300000))
    session_id = f'{today}-AM-001'
    for row in new_rows:
        if float(st.get('cash_krw', 0)) + 1e-9 < slot:
            break
        if buy_position(st, row, prices.get(row['market']), slot, 'AM', session_id, today):
            bought.append(row['coin'])

    st['cohort_id'] = session_id
    st['cohort_date'] = today
    st['last_rebalance_date'] = today
    st['source_snapshot_asof_kst'] = snap.get('asof_kst', asof.isoformat())
    st['strategy'] = CFG.get('paper_strategy', 'VPD_TOP10_EQUAL_WEIGHT')
    st['cohort_policy'] = 'AM_ROLLING_TOP10_PLUS_PM_REFILL'
    st['capital_model'] = 'FIXED_SLOT_KEEP_EXIT_REFILL'
    st['rebalance_start_equity_krw'] = equity_before
    st['realized_pnl_krw'] = 0.0
    save_state(st)
    telegram(
        '🔄 MAGI2 AM TOP10 리밸런싱 완료\n'
        f"{st['cohort_id']}\n"
        f"KEEP {len(kept)}: {', '.join(kept) if kept else '-'}\n"
        f"SELL {len(exited)}: {', '.join(exited) if exited else '-'}\n"
        f"BUY {len(bought)}: {', '.join(bought) if bought else '-'}\n"
        f'리밸런싱 직전 순자산 {equity_before:,.0f}원\n'
        '※ PM 리필 종목도 동일하게 KEEP/VPD_EXIT 심사 · PAPER ONLY'
    )
    return True


def maybe_refill_evening(st):
    loaded = load_snapshot_window(
        'evening_after_kst', 'evening_window_end_kst', '17:50', '18:20'
    )
    if loaded is None:
        return False
    snap, asof = loaded
    today = asof.date().isoformat()
    if st.get('last_evening_refill_date') == today:
        return False
    candidates = snap.get('top10', [])[:int(CFG.get('session', {}).get('top_n', 10))]
    if not candidates:
        return False

    slot = float(CFG.get('position_krw', 300000))
    cash = float(st.get('cash_krw', 0))
    if cash + 1e-9 < slot:
        return False

    active = {c: p for c, p in st.get('positions', {}).items() if p.get('status', 'OPEN') == 'OPEN'}
    new_rows = [row for row in candidates if row['coin'] not in active]
    if not new_rows:
        st['last_evening_refill_date'] = today
        st['source_evening_snapshot_asof_kst'] = snap.get('asof_kst', asof.isoformat())
        save_state(st)
        return False

    slots = int(cash // slot)
    selected = new_rows[:slots]
    prices = get_prices([row['market'] for row in selected])
    bought = []
    session_id = f'{today}-PM-REFILL'
    for row in selected:
        if float(st.get('cash_krw', 0)) + 1e-9 < slot:
            break
        if buy_position(st, row, prices.get(row['market']), slot, 'PM_REFILL', session_id, today):
            bought.append(row['coin'])

    if bought:
        st['last_evening_refill_date'] = today
        st['source_evening_snapshot_asof_kst'] = snap.get('asof_kst', asof.isoformat())
        st['last_evening_refill_session_id'] = session_id
        save_state(st)
        telegram(
            '🌙 MAGI2 EVENING REFILL 완료\n'
            f'{session_id}\n'
            f"BUY {len(bought)}: {', '.join(bought)}\n"
            f'잔여 예수금 {float(st.get("cash_krw", 0)):,.0f}원\n'
            '※ 기존 보유는 매도하지 않음\n'
            '※ 리필 종목은 다음 AM TOP10에서 즉시 KEEP/VPD_EXIT 심사\n'
            '※ 24시간 최소보유 없음 · PAPER ONLY'
        )
        return True
    return False


def monitor_once(st, send_status=False):
    active = {c: p for c, p in st.get('positions', {}).items() if p.get('status', 'OPEN') == 'OPEN'}
    prices = get_prices([p['market'] for p in active.values()])
    if send_status:
        telegram(portfolio_status(st, prices))
    tp_default = float(CFG['exit']['take_profit_pct'])
    sl_default = float(CFG['exit']['hard_stop_pct'])
    warning_default = float(CFG['exit']['warning_profit_pct'])
    for coin, p in list(active.items()):
        px = prices.get(p['market'])
        if px is None:
            continue
        p['last_price'] = px
        p['peak_price'] = max(float(p.get('peak_price', p['entry_price'])), px)
        ret = position_return(p, px)
        tp = float(p.get('target_profit_pct', tp_default))
        sl = float(p.get('stop_loss_pct', sl_default))
        warning = float(p.get('warning_profit_pct', warning_default))
        if ret >= tp:
            close_position(st, coin, p, px, 'TAKE_PROFIT')
        elif ret <= sl:
            close_position(st, coin, p, px, 'HARD_STOP')
        elif ret >= warning:
            telegram(
                '⚠️ MAGI2 PAPER 매도임박\n'
                f"{coin} / 매수금액 {int(p['cost_krw']):,}원 / 순수익률 {ret:+.2f}%\n"
                f'목표수익률 설정값 +{tp:.1f}% / 목표까지 {tp-ret:.2f}%p\n'
                '1분 단위 감시 중 · 비용 반영 · PAPER ONLY'
            )
    save_state(st)
    return prices


def main():
    st = load_state()
    if st.get('mode') != 'PAPER':
        raise RuntimeError('MAGI2 state is not PAPER mode')
    migrate_buy_fee(st)
    maybe_rebalance_morning(st)
    maybe_refill_evening(st)

    poll_seconds = int(CFG.get('monitor', {}).get('near_target_poll_seconds', 60))
    run_minutes = int(CFG.get('monitor', {}).get('run_window_minutes', 14))
    deadline = time.time() + run_minutes * 60

    monitor_once(st, send_status=True)
    while time.time() + poll_seconds <= deadline:
        time.sleep(poll_seconds)
        monitor_once(st, send_status=False)

    print(json.dumps({
        'mode': 'PAPER',
        'cohort_id': st.get('cohort_id'),
        'open_positions': sum(
            1 for p in st.get('positions', {}).values()
            if p.get('status', 'OPEN') == 'OPEN'
        ),
        'cash_krw': round(float(st.get('cash_krw', 0)), 2),
        'updated_at': st.get('updated_at'),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
