"""Fill VPD vacancies from the validated, local point-in-time scan."""
import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from magi2 import paper_engine as pe

KST = ZoneInfo('Asia/Seoul')
MIN_VPD = float(os.getenv('MAGI2_REFILL_MIN_VPD', '75'))
REASONS = {'TAKE_PROFIT', 'HARD_STOP'}


def same_day_cooldown(st, today):
    blocked = set()
    rows = [dict(p, coin=coin) for coin,p in st.get('positions', {}).items()
            if p.get('status') == 'CLOSED'] + st.get('closed_positions', [])
    for p in rows:
        if not p.get('coin') or p.get('exit_reason') not in REASONS:
            continue
        stamp = pe.parse_snapshot_asof(p.get('exit_at'))
        if stamp and stamp.date().isoformat() == today:
            blocked.add(p['coin'])
    return blocked


def candidate_rows(snap, active, blocked):
    def eligible(rows):
        result = []
        seen = set()
        for row in rows:
            coin = row.get('coin')
            market = row.get('market')
            try:
                score = float(row.get('VPD', 0))
            except (TypeError, ValueError):
                continue
            if (coin and market == 'KRW-' + coin and coin not in active and coin not in blocked
                    and coin not in seen and math.isfinite(score) and score >= MIN_VPD):
                seen.add(coin)
                result.append(row)
        return sorted(result, key=lambda r: (float(r.get('Rank', 999999)), -float(r['VPD']), r['coin']))
    top = eligible(snap.get('top10', []))
    if top:
        return top, '현재시점 TOP10'
    rows = eligible((snap.get('all_rows') or {}).values())
    return rows, '현재시점 VPD 차순위'


def refill(st=None, expected_asof=None):
    # CLI callers without a supplied scan get the same public-data scanner.
    # The Telegram path scans in the background before taking the PAPER worker.
    if expected_asof is None:
        snap = pe.build_snapshot(pe.STATE_PATH.resolve().with_name('vpd_rebalance.json'), pe.now_dt())
        expected_asof = snap['asof']
    loaded = pe.load_morning_snapshot(expected_asof)
    if not loaded:
        raise RuntimeError('REFILL_SNAPSHOT_NOT_READY: 요청 시점 VPD 자료가 없거나 만료됐습니다.')
    snap, asof = loaded
    issue = pe.snapshot_issue(asof, pe.now_dt(), snap)
    if issue:
        raise RuntimeError('REFILL_SNAPSHOT_EXPIRED: ' + issue)
    # Reload after the scan: the protective monitor may have sold positions.
    st = pe.load_state() if st is None else st
    pe.migrate_state(st)
    today = pe.now_dt().date().isoformat()
    top_n = int(pe.CFG.get('session', {}).get('top_n', 10))
    active = {c:p for c,p in st.get('positions', {}).items() if p.get('status') == 'OPEN'}
    vacant = max(0, top_n-len(active))
    cash = float(st.get('cash_krw', 0))

    def no_buy(reason):
        print(f'VPD refill no_trade: {reason} asof={asof.isoformat()} vacant={vacant} cash={cash:.2f}', flush=True)
        pe.telegram(f'ℹ️ VPD 리필 · 매수 없음\n{reason}\n빈자리 {vacant} / 예수금 {cash:,.0f}원\n자료 기준 {snap.get("asof_kst", asof.isoformat())}\nPAPER ONLY')
        pe.send_current_status(st)
        return False

    if vacant == 0:
        return no_buy('이미 최대 보유 종목 수에 도달했습니다.')
    slot = pe.derive_daily_equal_buy(st, today)
    if not slot or not math.isfinite(slot) or slot <= 0:
        return no_buy('당일 균등매수 기준금액을 확인할 수 없습니다. 리밸런싱으로 배분 기준을 설정해야 합니다.')
    count = min(vacant, max(0, int((cash+1e-9)//slot)))
    if count == 0:
        return no_buy(f'한 종목 배분금 {slot:,.0f}원보다 예수금이 부족합니다.')
    blocked = same_day_cooldown(st, today)
    rows, source = candidate_rows(snap, active, blocked)
    if not rows:
        return no_buy(f'현재 VPD {MIN_VPD:g}+ 신규 후보가 없습니다. 당일 TP/SL 재진입 제외: '+(', '.join(sorted(blocked)) or '-'))
    rows = rows[:count]
    prices = pe.get_prices([r['market'] for r in rows])
    if pe.load_morning_snapshot(expected_asof) is None or pe.snapshot_issue(asof, pe.now_dt(), snap):
        raise RuntimeError('REFILL_SNAPSHOT_EXPIRED: 가격 조회 중 VPD 자료가 만료됐습니다. 매수하지 않았습니다.')
    sid = f'{today}-REFILL-{pe.now_dt():%H%M%S}'
    bought = []
    for row in rows:
        px = prices.get(row['market'])
        if px is None or not math.isfinite(float(px)) or float(px) <= 0:
            continue
        if pe.buy_position(st, row, px, slot, 'REFILL', sid, today):
            bought.append(row['coin'])
    if not bought:
        return no_buy('적격 후보의 유효한 현재가격을 확보하지 못했습니다.')
    st.update(last_refill_at=pe.now_iso(), last_refill_session_id=sid,
              source_refill_snapshot_asof_kst=snap.get('asof_kst', asof.isoformat()))
    pe.save_state(st)
    print(f'VPD refill completed: asof={asof.isoformat()} bought={",".join(bought)} cash={st["cash_krw"]:.2f}', flush=True)
    details = '\n'.join(f'{i+1}. {coin} | 매수금액 {slot:,.0f}원' for i,coin in enumerate(bought))
    pe.telegram(f'♻️ VPD 리필 완료\n{sid}\n자료 기준 {snap.get("asof_kst", asof.isoformat())}\n후보소스 {source}\nBUY {len(bought)}\n{details}\n잔여 예수금 {st["cash_krw"]:,.0f}원\nPAPER ONLY')
    pe.send_current_status(st, '📊 REFILL 후 VPD 모의투자 현황 [PAPER]')
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-asof')
    args = parser.parse_args()
    refill(expected_asof=args.snapshot_asof)
