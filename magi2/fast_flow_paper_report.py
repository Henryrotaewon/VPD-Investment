"""Read-only views of the independent FAST PAPER ledger."""
from datetime import datetime
import json
from magi3.accounts import quantity
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000, ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')


def keyboard(home=False):
    rows = [
        [{'text':'📊 자산현황','callback_data':'nav:fast_paper_balance'},
         {'text':'🔎 포착·추적','callback_data':'nav:fast_watch'}],
        [{'text':'📒 매매기록','callback_data':'nav:fast_paper_orders'},
         {'text':'📅 일별평가','callback_data':'nav:fast_paper_daily'}]]
    back = [{'text':'↩️ 메인 메뉴','callback_data':'nav:menu'}]
    if not home:
        back.insert(0, {'text':'↩️ FAST 모의투자','callback_data':'nav:fast_paper'})
    return {'inline_keyboard': rows + [back]}


def menu():
    return ('⚡ FAST 모의투자 [PAPER]\n'
            '업비트 · 초기 300만원 · 최대 10종목 분할\n\n'
            '자산현황: 예수금·보유 종목·수익률\n'
            '포착·추적: 포착 종목과 이후 가격 변화\n'
            '매매기록: 체결 내역·매수 제외 사유\n'
            '일별평가: 날짜별 평가금액·누적손익\n\n'
            '아래에서 확인할 항목을 선택해 주세요.', keyboard(home=True))


def view(state_dir, section='fast_paper'):
    path = Path(state_dir)/'fast-observe'/'paper-v1.sqlite3'
    if not path.exists():
        return '⚡ FAST 모의투자 [PAPER]\n장부 초기화 대기', keyboard()
    try:
        db = sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True, timeout=2)
        try:
            row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            if not row:
                return '⚡ FAST 모의투자 [PAPER]\n장부 초기화 대기', keyboard()
            s = json.loads(row[0]); policy = s['policy']; now = int(time.time()*1000)
            if section == 'fast_paper_orders':
                return orders_report(db, s, now), keyboard()
            lines = ['⚡ FAST 모의투자 [PAPER]', '검증 후보: '+policy['version'],
                     f"시작 {clock(s['started_ms'])} · 갱신 {max(0,now-s['updated_ms'])//1000}초 전"]
            if s.get('entries_paused'):lines.append('⏸ 포착·신규 매매 중지 · 기존 보유 보호청산만 유지')
            if not s.get('entries_paused'):lines.append(f"매수가 상한: 새 포착가 +{policy['entry_cap']*100:g}% · 대기 10초")
            if policy.get('retry_unfilled') and not s.get('entries_paused'):
                lines.append('미체결: 새 신호 재시도 · 매수 체결 후 당일 재매수 금지 (09시 기준)')
            if now-s['updated_ms'] > 30000:
                lines.append('⚠ 원장 갱신 지연 · 아래 평가는 마지막 관측 기준')
            if section == 'fast_paper_daily':
                rows = db.execute('SELECT day,ts,payload FROM daily ORDER BY day DESC LIMIT 7').fetchall()
                lines.append('업비트 일자: 09:00 KST 시작 · 당일은 진행 중')
                for day, ts, payload in rows:
                    r = json.loads(payload)
                    lines.append(f'{clock(day*86400000)[:5]} · 평가 {r["equity"]:,.0f}원 · 누적 {r["return_pct"]:+.2f}%\n실현 누계 {r["realized"]:+,.0f}원 · 기준 {clock(ts)}'+(' · 지연 시세 포함' if r['stale_marks'] else ''))
                if not rows: lines.append('첫 평가 기록 대기')
            else:
                positions = s['positions']; pending = s['pending']
                reserved = sum(p['budget'] for p in pending.values())
                nav = s['cash'] + sum(p['qty']*p['mark'] for p in positions.values())
                lines += [f"최초원금 {policy['initial']:,.0f}원 · {policy['slots']}분할",
                          f"보유 {len(positions)}/{policy['slots']} · 매수대기 {len(pending)}",
                          f"예수금 {s['cash']:,.0f}원 · 예약 {reserved:,.0f}원",
                          f"평가 {nav:,.0f}원 · 누적 {(nav/policy['initial']-1)*100:+.2f}%",
                          f"실현손익 {s['realized']:+,.0f}원 · 완료매매 {s['closed']}회"]
                vacant = policy['slots']-len(positions)-len(pending)
                if vacant and not s.get('entries_paused'): lines.append(f"다음 배분 {(s['cash']-reserved)/vacant:,.0f}원")
                for symbol, p in positions.items():
                    ret = (p['qty']*p['mark']/p['cost']-1)*100
                    suffix = ' · 시세 지연' if now-p['mark_ms']>2000 else ''
                    if p.get('exit'): suffix += ' · 매도 대기'
                    lines.append(f"{symbol} | {quantity(p['qty'])}개 | 원가 {p['cost']:,.0f}원 | 순평가 {ret:+.2f}% | 보호 {price_text(p['stop'])}{suffix}")
                if not positions and not pending: lines.append('중지 상태 · 전액 현금' if s.get('entries_paused') else '새 신호 대기 · 10종목 강제 매수 없음')
            if section not in ('fast_paper_orders', 'fast_paper_daily'):
                lines.extend(experiment_summary(path.parent, now))
            lines.append('공개 호가 모의체결 · 수수료/슬리피지 각 편도 0.05% 가정 · 실제 주문 없음')
            return '\n'.join(lines), keyboard()
        finally:
            db.close()
    except (sqlite3.Error, ValueError, KeyError):
        return '⚡ FAST 모의투자 [PAPER]\n원장 조회 대기 · 잠시 후 다시 조회', keyboard()


def experiment_summary(directory, now):
    try:
        status = json.loads((directory/'status.json').read_text())
        pair = (status.get('observation') or {}).get('experiment')
        if not pair:
            return []
        lines = ['\n🧪 조기 진입 비교실험'+(' 중지 · 과거 성과' if status.get('observation',{}).get('fast_disabled') else ' · 각 300만원 독립 계좌'),
                 '완료 5분봉 기준 동일 · 확인 횟수만 비교']
        for key, label in [('control', '기존 조건 2회'), ('early', '조기 조건 1회')]:
            r = pair[key]
            lines.append(f"{label} · 시작 {clock(r['started_ms'])}\n"
                         f"순평가 {r['return_pct']:+.2f}% · 실현 {r['realized']:+,.0f}원 · 완료 {r['closed']}회 · 보유 {r['positions']}\n"
                         f"정상 매도 {r['normal_exit_pnl']:+,.0f}원 / 자료단절 매도 {r['gap_exit_pnl']:+,.0f}원")
            if now-r['asof_ms'] > 120000 or r['stale_marks']:
                lines.append('⚠ 비교 평가 갱신·시세 지연')
        return lines
    except (OSError, ValueError, KeyError, TypeError):
        return ['병행 검증 상태 조회 대기']


REASONS = {
    'USER_STOPPED': '사용자 요청으로 신규 매수 중지',
    'PROTECTION': '보호선 도달(초기·추적 보호선)',
    'NET_SELL_20S_BELOW_VWAP': '20초 순매도 + 진입 후 평균체결가(VWAP) 하회',
    'NO_NEW_HIGH_3M_NONPOSITIVE': '3분간 고점 갱신 없음 + 순수익 미확보',
    'INVALID_PROTECTION': '유효한 초기 보호선 없음',
    'PROTECTION_ALREADY_BROKEN': '매수 전 보호선 이탈',
    'ENTRY_PRICE_CAP': '매수가 상한 초과',
    'INSUFFICIENT_VISIBLE_DEPTH': '호가 잔량 부족',
    'BASELINE_NOT_READY': '판단용 기초자료 대기',
    'ALREADY_BOUGHT_TODAY': '당일 매수 이력 있음',
    'NO_SLOT': '빈 슬롯 없음',
    'INSUFFICIENT_CASH': '가용현금 부족',
    'WAIT_FRESH_BOOK': '신선한 호가 확보 대기',
}


def reason_text(reason):
    if reason.startswith('DATA_GAP_'):
        return {
            'DATA_GAP_RESTART': '재시작으로 관측 단절',
            'DATA_GAP_STALE_FEED': '체결·호가 갱신 지연',
            'DATA_GAP_PROCESS_GAP': '처리 지연으로 관측 단절',
            'DATA_GAP_INVALID_MESSAGE': '수신자료 오류',
        }.get(reason, '연결 끊김으로 관측 단절')
    return REASONS.get(reason, reason)


def price_text(value):
    # Upbit KRW prices: preserve up to eight decimals without exponent notation.
    return f'{value:,.8f}'.rstrip('0').rstrip('.')


def elapsed_text(start, end):
    seconds = max(0, int((end-start)//1000))
    if seconds >= 3600:
        return f'{seconds//3600}시간 {seconds%3600//60}분'
    if seconds >= 60:
        return f'{seconds//60}분 {seconds%60}초'
    return f'{seconds}초'


def orders_report(db, state, now):
    # Each BUY starts a trade. Bound SELL aggregation by the next BUY in the
    # same symbol so repeated days and partial exits cannot merge together.
    cursor = db.execute("""
        WITH buys AS (
            SELECT id,ts,symbol,quantity,price,cash,
                   LEAD(id) OVER (PARTITION BY symbol ORDER BY id) next_id
            FROM fills WHERE side='BUY'
        )
        SELECT b.id,b.ts,b.symbol,b.quantity,b.price,b.cash,
               COALESCE(SUM(f.quantity),0) sold_qty,
               COALESCE(SUM(f.quantity*f.price),0) sold_value,
               COALESCE(SUM(f.pnl),0) pnl, MAX(f.ts) exit_ms,
               COUNT(f.id) sell_fills, GROUP_CONCAT(DISTINCT f.reason) reasons,
               COALESCE(MAX(f.id),b.id) latest_id
        FROM buys b LEFT JOIN fills f
          ON f.symbol=b.symbol AND f.side='SELL' AND f.id>b.id
         AND (b.next_id IS NULL OR f.id<b.next_id)
        GROUP BY b.id ORDER BY latest_id DESC LIMIT 6
    """)
    columns = [c[0] for c in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    policy = state['policy']
    lines = ['⚡ FAST 매매기록 [PAPER]',
             f"누적 실현 {state['realized']:+,.0f}원 · 완료 {state['closed']}회",
             f"최근 거래 {len(rows)}건 · 최종 체결순 · 갱신 {max(0,now-state['updated_ms'])//1000}초 전"]
    if state.get('entries_paused'):lines.append('⏸ 포착·신규 매매 중지 · 성과 이력 보존')
    if now-state['updated_ms'] > 30000:
        lines.append('⚠ 원장 갱신 지연 · 마지막 관측 기준')
    for r in rows:
        sold = r['sold_qty'] > 0
        closed = sold and r['quantity']-r['sold_qty'] <= 1e-8
        label = '매도완료' if closed else '일부 매도·보유 중' if sold else '보유 중'
        icon = ('🟢' if r['pnl'] > 0 else '🔴' if r['pnl'] < 0 else '⚪') if closed else '🟡'
        lines += ['', f"{icon} {r['symbol']} · {label}"]
        if closed:
            ret = r['pnl']/abs(r['cash'])*100 if r['cash'] else 0
            lines.append(f"순손익 {r['pnl']:+,.0f}원 ({ret:+.2f}%) · 보유 {elapsed_text(r['ts'],r['exit_ms'])}")
        elif sold:
            lines.append(f"실현손익 {r['pnl']:+,.0f}원 · 잔여 {quantity(max(0,r['quantity']-r['sold_qty']))}개")
        else:
            lines.append(f"보유 {elapsed_text(r['ts'],now)} · 미실현 평가는 자산현황에서 확인")
        lines.append(f"매수 {clock(r['ts'])} · {price_text(r['price'])}원")
        if sold:
            average = r['sold_value']/r['sold_qty']
            prefix = '매도 평균' if r['sell_fills'] > 1 else '매도'
            lines.append(f"{prefix} {clock(r['exit_ms'])} · {price_text(average)}원")
        lines.append(f"매수금액 {abs(r['cash']):,.0f}원 · 수량 {quantity(r['quantity'])}개")
        if sold:
            lines.append('청산: '+' / '.join(reason_text(reason) for reason in r['reasons'].split(',')))
    if not rows:
        lines += ['', '체결 기록 없음 · 새 포착 대기']
    skips = db.execute("""
        SELECT symbol,reason,ts FROM (
            SELECT symbol,reason,ts,status,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY ts DESC) latest
            FROM signals
        ) WHERE latest=1 AND status='SKIPPED' ORDER BY ts DESC LIMIT 5
    """).fetchall()
    if skips:
        lines += ['', '🚫 최근 매수 제외 · 종목별 최신 기록']
        lines += [f'{symbol} · {reason_text(reason)} ({clock(ts)})' for symbol,reason,ts in skips]
    lines += ['', f"운용 시작 {clock(state['started_ms'])} · {policy['version']}",
              f"매수 상한 +{policy['entry_cap']*100:g}% · 최대 10초 대기 · 체결 후 당일 재매수 금지(09시 기준)",
              '미체결은 새 신호에서 재시도' if policy.get('retry_unfilled') else '미체결 재시도 없음',
              '순손익: 수수료·슬리피지 반영 · 매수금액: 수수료 포함',
              '수수료·슬리피지 각 편도 0.05% 가정 · 공개 호가 모의체결 · 실제 주문 없음']
    return '\n'.join(lines)
