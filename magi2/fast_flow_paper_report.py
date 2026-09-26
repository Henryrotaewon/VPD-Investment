"""Read-only views of the independent FAST PAPER ledger."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


def clock(stamp):
    return datetime.fromtimestamp(stamp/1000, ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')


def keyboard():
    return {'inline_keyboard': [
        [{'text':'📊 FAST 자산현황','callback_data':'nav:fast_paper'},
         {'text':'📒 FAST 매매기록','callback_data':'nav:fast_paper_orders'}],
        [{'text':'📅 FAST 일별평가','callback_data':'nav:fast_paper_daily'},
         {'text':'🔎 포착·추적','callback_data':'nav:fast_watch'}]]}


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
            lines = ['⚡ FAST 모의투자 [PAPER]', '검증 후보: '+policy['version'],
                     f"시작 {clock(s['started_ms'])} · 갱신 {max(0,now-s['updated_ms'])//1000}초 전"]
            if now-s['updated_ms'] > 30000:
                lines.append('⚠ 원장 갱신 지연 · 아래 평가는 마지막 관측 기준')
            if section == 'fast_paper_orders':
                rows = db.execute('SELECT ts,symbol,side,quantity,price,pnl,reason FROM fills ORDER BY id DESC LIMIT 12').fetchall()
                for ts, symbol, side, qty, price, pnl, reason in rows:
                    lines.append(f'{clock(ts)} {symbol} {side}\n{qty:.8g}개 × {price:g}원 · 실현 {pnl:+,.0f}원\n{reason}')
                if not rows: lines.append('체결 기록 없음 · 시작 후 새 포착 대기')
                skips = db.execute("SELECT symbol,reason FROM signals WHERE status='SKIPPED' ORDER BY ts DESC LIMIT 5").fetchall()
                if skips:
                    lines.append('최근 매수 제외')
                    lines.extend(f'{symbol}: {reason}' for symbol,reason in skips)
            elif section == 'fast_paper_daily':
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
                if vacant: lines.append(f"다음 배분 {(s['cash']-reserved)/vacant:,.0f}원")
                for symbol, p in positions.items():
                    ret = (p['qty']*p['mark']/p['cost']-1)*100
                    suffix = ' · 시세 지연' if now-p['mark_ms']>2000 else ''
                    if p.get('exit'): suffix += ' · 매도 대기'
                    lines.append(f"{symbol} | 원가 {p['cost']:,.0f}원 | 순평가 {ret:+.2f}% | 보호 {p['stop']:g}{suffix}")
                if not positions and not pending: lines.append('새 신호 대기 · 10종목 강제 매수 없음')
            lines.append('공개 호가 모의체결 · 수수료/슬리피지 각 편도 0.05% 가정 · 실제 주문 없음')
            return '\n'.join(lines), keyboard()
        finally:
            db.close()
    except (sqlite3.Error, ValueError, KeyError):
        return '⚡ FAST 모의투자 [PAPER]\n원장 조회 대기 · 잠시 후 다시 조회', keyboard()
