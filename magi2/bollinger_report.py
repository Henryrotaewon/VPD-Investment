"""Read-only, paginated round trips from the Double Bollinger PAPER ledger."""
import json
import math
from pathlib import Path
import sqlite3

from magi2.fast_flow_paper_report import clock, elapsed_text, price_text, reason_text as gap_reason
from magi2.paper_performance import readonly
from magi3.accounts import quantity

PAGE_SIZE = 5
REASONS = {
    'DOUBLE_BB_CCI_BREAKOUT': '밴드 수축 후 상단 돌파·CCI·거래대금 조건 충족',
    'PROTECTION': '초기 보호선(신호 포함 최근 6봉 저점) 이탈',
    'BB_MIDDLE_AND_CCI_NEGATIVE': '5분 종가가 단기 중심선 하회 + CCI 음수',
}


def reason_text(reason):
    return REASONS.get(reason, gap_reason(reason))


def keyboard(anchor=0, offset=0, total=0):
    nav = []
    if offset:
        nav.append(dict(text='◀ 최근', callback_data=f'bollinger_orders:{anchor}:{max(0,offset-PAGE_SIZE)}'))
    nav.append(dict(text='새로고침', callback_data='nav:bollinger_orders'))
    if offset+PAGE_SIZE < total:
        nav.append(dict(text='이전 거래 ▶', callback_data=f'bollinger_orders:{anchor}:{offset+PAGE_SIZE}'))
    return {'inline_keyboard': [nav,
        [dict(text='📊 더블볼린저·CCI 결과', callback_data='performance:bollinger:0')],
        [dict(text='↩️ 전략검증', callback_data='nav:strategies'),
         dict(text='↩️ 메인 메뉴', callback_data='nav:menu')]]}


def trades_page(db, anchor, offset):
    # BUY identity, not symbol/day, separates same-day reentries. The fill-ID
    # anchor keeps pages stable even when new fills arrive between button taps.
    return db.execute('''
        WITH buys AS (
            SELECT id,ts,symbol,quantity,price,cash,reason,
                   LEAD(id) OVER (PARTITION BY symbol ORDER BY id) next_id
            FROM fills WHERE side='BUY' AND id<=?
        )
        SELECT b.id,b.ts,b.symbol,b.quantity,b.price,b.cash,b.reason,
               COALESCE(SUM(f.quantity),0) sold_qty,
               COALESCE(SUM(f.quantity*f.price),0) sold_value,
               COALESCE(SUM(f.cash),0) proceeds,
               COALESCE(SUM(f.pnl),0) pnl, MAX(f.ts) exit_ms,
               COUNT(f.id) sell_fills, GROUP_CONCAT(DISTINCT f.reason) exit_reasons,
               COALESCE(MAX(f.id),b.id) latest_id
        FROM buys b LEFT JOIN fills f
          ON f.symbol=b.symbol AND f.side='SELL' AND f.id>b.id AND f.id<=?
         AND (b.next_id IS NULL OR f.id<b.next_id)
        GROUP BY b.id ORDER BY latest_id DESC LIMIT ? OFFSET ?
    ''', (anchor, anchor, PAGE_SIZE, offset)).fetchall()


def view(state_dir, now, anchor=0, offset=0):
    path = Path(state_dir)/'bollinger-paper'/'v1.sqlite3'
    title = '📒 더블볼린저·CCI 매매이력 [PAPER]'
    if not path.exists():
        return title+'\n모의 원장 준비 중 · 체결 이력 조회 대기', keyboard()
    try:
        db = readonly(path)
        try:
            state = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            latest = db.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
            anchor = min(anchor, latest) if anchor > 0 else latest
            total = db.execute("SELECT count(*) FROM fills WHERE side='BUY' AND id<=?", (anchor,)).fetchone()[0]
            offset = min(max(0, offset)//PAGE_SIZE*PAGE_SIZE, max(0,(total-1)//PAGE_SIZE)*PAGE_SIZE)
            rows = trades_page(db, anchor, offset)
            last = db.execute('SELECT ts FROM fills WHERE id=?', (anchor,)).fetchone()
            realized = db.execute("SELECT COALESCE(SUM(pnl),0) FROM fills WHERE side='SELL' AND id<=?", (anchor,)).fetchone()[0]
            lines = [title, '업비트 KRW · 모든 시각 KST · 최종 체결 최신순',
                     f'기준시점 누적 실현손익 {realized:+,.0f}원 · 매수 거래 {total}건',
                     f'{offset//PAGE_SIZE+1}/{max(1,math.ceil(total/PAGE_SIZE))}페이지 · 페이지당 {PAGE_SIZE}건']
            if last:
                lines.append('체결 기준 '+clock(last['ts'])+' · 다시 열면 최신 이력 반영')
            if now-state['updated_ms'] > 30000:
                lines.append('원장 갱신 지연 · 저장된 이력 기준')
            for r in rows:
                sold = r['sold_qty'] > 0
                remaining = max(0., r['quantity']-r['sold_qty'])
                closed = sold and remaining <= 1e-8
                label = '매도완료' if closed else '일부 매도·보유 중' if sold else '보유 중'
                lines += ['', f"• {r['symbol']} · {label}",
                          f"매수 {clock(r['ts'])} · {price_text(r['price'])}원",
                          f"매수금액 {abs(r['cash']):,.0f}원 · 수량 {quantity(r['quantity'])}개",
                          '진입: '+reason_text(r['reason'])]
                if sold:
                    label = f"매도 {r['sell_fills']}회 평균 · 최종" if r['sell_fills'] > 1 else '매도'
                    lines += [f"{label} {clock(r['exit_ms'])} · {price_text(r['sold_value']/r['sold_qty'])}원",
                              f"매도수령액 {r['proceeds']:,.0f}원 · 실현손익 {r['pnl']:+,.0f}원"]
                    if closed:
                        ret = r['pnl']/abs(r['cash'])*100 if r['cash'] else 0.
                        lines.append(f"순수익률 {ret:+.2f}% · 보유 {elapsed_text(r['ts'],r['exit_ms'])}")
                    else:
                        lines.append(f"잔여 수량 {quantity(remaining)}개 · 청산 진행 중")
                    lines.append('청산: '+' / '.join(reason_text(s) for s in r['exit_reasons'].split(',')))
                else:
                    lines.append('매도 체결 없음 · 실현손익 미확정')
            if not rows:
                lines += ['', '체결 기록 없음 · 새 매수 체결 대기']
                if state['pending']:
                    lines.append(f"현재 매수 대기 {len(state['pending'])}종목 · 미체결은 매매이력에 포함하지 않음")
            lines += ['', '매수금액: 수수료 포함 · 매도수령액: 수수료 차감',
                      '순손익·순수익률: 수수료/슬리피지 반영 · 분할매도 합산',
                      '전량 청산 후 새 수축 신호에 재진입 가능 · 실제 주문 없음']
            return '\n'.join(lines), keyboard(anchor, offset, total)
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
        return title+'\n원장 조회 대기 · 잠시 후 다시 열어 주세요.', keyboard()
