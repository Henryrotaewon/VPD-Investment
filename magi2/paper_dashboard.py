"""Unified read-only navigation over the existing independent PAPER ledgers."""
import json
import sqlite3
from collections import deque
from pathlib import Path

from magi2.paper_performance import (load_results, readonly, clock, timestamp,
                                     win_text, return_text)

NAMES = {'vpd': 'VPD', 'fast': 'FAST base', 'indicator': '지표가속',
         'bollinger': '더블볼린저·CCI', 'bear': 'FAST-BEAR',
         'derivatives': 'FAST-DERIVATIVES'}
SECTIONS = {'results': '투자결과', 'status': '투자전략세부', 'guide': '전략 설명'}
PAGE_SIZE = 4


def button(text, data):
    return dict(text=text, callback_data=data)


def keyboard(section=None, key=None, offset=0, total=0):
    rows = []
    if section and not key:
        choices = [button(name, f'paper:{section}:{k}:0') for k, name in NAMES.items()]
        rows += [choices[i:i+2] for i in range(0, len(choices), 2)]
        rows.append([button('Shadow 검증', 'nav:shadows')])
    if key:
        nav = []
        if offset:
            nav.append(button('◀ 이전', f'paper:{section}:{key}:{max(0,offset-PAGE_SIZE)}'))
        if offset+PAGE_SIZE < total:
            nav.append(button('다음 ▶', f'paper:{section}:{key}:{offset+PAGE_SIZE}'))
        if nav:
            rows.append(nav)
        if section == 'status':
            if key == 'vpd':
                rows.append([button('VPD 분석·수동 운용', 'nav:vpd')])
            elif key == 'indicator':
                rows.append([button('포착 이력', 'nav:fast_captures'), button('수동 운용', 'nav:indicator')])
            elif key == 'fast':
                rows.append([button('포착·추적', 'nav:fast_watch'), button('매매 상세', 'nav:fast_paper_orders')])
            elif key == 'bollinger':
                rows.append([button('매매 상세', 'nav:bollinger_orders')])
        rows.append([button(label, f'paper:{s}:{key}:0') for s, label in SECTIONS.items() if s != section])
        rows.append([button('↩️ '+SECTIONS[section], 'nav:paper_'+section)])
    else:
        rows += [[button(label, 'nav:paper_'+s)] for s, label in SECTIONS.items() if s != section]
    rows.append([button('↩️ 모의투자현황', 'nav:paper')] if section else
                [button('↩️ 메인 메뉴', 'nav:menu')])
    return {'inline_keyboard': rows}


def menu(root, now):
    """The first PAPER screen is the actual report, with the bottom navigation."""
    from magi2.telegram_ui import paper_keyboard
    text, _ = view(root, now, 'results')
    return text, paper_keyboard()


def money(value):
    return '확인 대기' if value is None else f'{value:,.0f}원'


def result_lines(root, key, now, compact=False):
    lines = ['• '+NAMES[key]]
    if key in ('bear', 'derivatives'):
        from magi2.fast_models.report import load, wins
        try:
            _, m, _, trades = load(root, key)
            if m['initial'] <= 0:
                raise ValueError('INVALID_INITIAL_CAPITAL')
            fresh = m['valid'] and 0 <= now-m['last_ms'] <= 120000
            lines += [f"투자원금 {money(m['initial'])} · 평가 {money(m['equity'])}",
                      f"수익률 {(m['equity']/m['initial']-1)*100:+.2f}% · 승률 {wins(trades)}"+('' if fresh else ' · 최근 관측값'),
                      f"MDD {m['mdd']:.2f}%"+(' · 관측 공백 포함' if m['gap'] else ''),
                      f"시작 {clock(m['started_ms'])} · 평가 {clock(m['last_ms'])}"+('' if fresh else ' · 최근 관측값')]
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, IndexError):
            lines.append('원장 준비 중 · 성과 확인 대기')
        return lines[:3] if compact else lines
    r = load_results(root, key, now)
    base_label = ('기준원금*' if compact else '성과 기준자산') if key == 'fast' else '투자원금'
    equity = r.current.equity if r.current else None
    lines += [f'{base_label} {money(r.initial)} · 평가 {money(equity)}',
              f'수익률 {return_text(r.current,r.initial)} · 승률 {win_text(r.trades,r.wins_complete)}']
    if compact:
        return lines
    if r.started:
        lines.append(f'시작 {clock(r.started)} · 평가 {clock(r.current.ts) if r.current else "대기"}')
    if key == 'fast':
        lines.append('재시작 기준 · 과거 성과 제외' if any('재개 이후' in note for note in r.notes) else '현재 원장 기준')
    if r.current and not r.current.valid:
        lines.append('최근 관측값 · 시세 갱신 대기')
    return lines


def guide(key):
    if key == 'vpd':
        from magi2.strategy_guide import strategy_text
        return strategy_text('vpd')
    if key == 'indicator':
        from magi2.hourly_indicator import GUIDE
        return GUIDE
    if key == 'fast':
        return ('FAST base · 단기 수급 추종 [PAPER]\n\n'
                '포착: 완료 10초 구간에서 거래대금 증가·매수 우위·호가 수급을 두 번 연속 확인합니다. '
                '5분 거래대금은 과거 10일 같은 시간대의 2배 이상, 단기 급증은 3배 이상, 매수 비중 65% 이상을 요구합니다.\n'
                '매수: 포착 이후 공개 호가로 모의체결. 포착가 +1.5% 상한, 최대 10초 대기, 잔량 부족·오래된 호가는 보류합니다.\n'
                '매도: 초기·추적 보호선, 20초 순매도와 진입 후 VWAP 하회, 3분 고점 미갱신과 순수익 미확보 시 청산합니다.\n'
                '목표: 고정 익절률 없음 · 수급과 보호선으로 청산\n'
                '최대 10종목 · 체결 후 같은 거래일 재매수 금지(09시 기준)\n'
                '수수료·슬리피지 각각 편도 0.05% · 재시작 이후 성과만 집계')
    if key == 'bollinger':
        return ('더블볼린저·CCI [PAPER]\n\n'
                '포착: 완료 5분봉 BB(20,2)·BB(60,2)의 수축 이후 상단 돌파, CCI(10)와 거래대금 조건을 확인합니다.\n'
                '매수: 신호 이후 공개 호가에서 가격 상한·잔량·신선도를 확인해 모의체결합니다. 과거 신호는 소급 매수하지 않습니다.\n'
                '매도: 신호 포함 최근 6봉 저점 보호선 이탈, 또는 단기 밴드 중심선 하회와 CCI 음수 시 청산합니다.\n'
                '목표: 고정 익절률 없음 · 밴드와 보호선 기준\n'
                '독립 원금 300만원 · 최대 10종목 · 수수료·슬리피지 각각 편도 0.05%')
    if key == 'bear':
        return ('FAST-BEAR · 하락장 반등 [PAPER]\n\n'
                '포착: BTC 하락 국면에서 완료 5분봉 RSI가 30 위로 회복하고 VWAP·매수세를 확인합니다. '
                '다음 완료봉에서도 저점·VWAP·매수 비중 60% 이상을 확인합니다.\n'
                '매수: 확인 후 새 호가 · 포착가 +0.3% 상한 · 10초 대기\n'
                '목표: 매수 체결가 대비 호가 +4% · +2%부터 +1% 보호선\n'
                '매도: 보호선, VWAP 반등 실패·매수세 약화 또는 1시간 경과\n'
                '보호 거리 최대 1.5% 조건 · 실제 체결 손실 상한을 보장하지 않습니다.\n'
                '독립 원금 300만원 · 최대 10종목 · 수수료·슬리피지 각각 편도 0.05%')
    return ('FAST-DERIVATIVES · 현선물 [PAPER]\n\n'
            '포착: 하락 추세 숏, 또는 현물·선물 가격차의 비용 차감 후 수렴 여력을 확인합니다. '
            'BTC·ETH·XRP·SOL·NEAR를 관측합니다.\n'
            '매수·진입: 신호 이후 새 호가로 모의체결. 현선물은 현물 매수와 무기한 선물 매도를 한 쌍으로 운용합니다.\n'
            '목표: 숏 가격 하락 3% / 현선물 쌍 순수익 +0.5% 또는 가격차 수렴\n'
            '매도·청산: 숏 역행 1.5%·4시간 제한, 현선물 쌍 −0.5%·24시간 제한, 담보 보호 조건\n'
            '거래소별 가상 지갑에 총 300만원 분산 · 담보를 계좌 간 합산하지 않습니다.\n'
            '호가 깊이·수수료·슬리피지·환산·펀딩 반영 · 실제 선물 주문 없음')


def state_path(root, key):
    if key in ('bear', 'derivatives'):
        return Path(root)/'fast-models-v1'/(key+'.sqlite3')
    if key == 'indicator':
        from magi2.hourly_indicator import COHORT
        return Path(root)/'indicator_paper'/COHORT/'ledger.sqlite3'
    return Path(root)/('fast-observe' if key == 'fast' else 'bollinger-paper')/('paper-v1.sqlite3' if key == 'fast' else 'v1.sqlite3')


def when(value):
    if not value:
        return '기록 없음'
    return clock(timestamp(value) if isinstance(value,str) else value)


def target(key, p):
    if key == 'vpd':
        return f"목표 +{p.get('target_profit_pct',12):g}% · 손절 {p.get('stop_loss_pct',-6):g}%"
    if key == 'bear':
        return '목표 +4% (가격 기준) · +2%부터 +1% 보호'
    if key == 'derivatives':
        return '목표 가격 하락 3%' if p.get('kind') == 'SHORT' else '목표 순익 +0.5% 또는 가격차 수렴'
    return '목표 고정 없음 · '+('추세·보호선 청산' if key == 'indicator' else '수급·보호선 청산' if key == 'fast' else '밴드·보호선 청산')


def status_data(root, key, now):
    """Return holding blocks and bounded recent trades; never instantiate an engine."""
    holdings, recent = [], []
    if key == 'vpd':
        path = Path(root)/'paper_state.json'
        before = path.stat().st_mtime_ns
        s = json.loads(path.read_text())
        if (Path(root)/'paper_rebuild_pending.json').exists():
            raise ValueError('REBUILD_IN_PROGRESS')
        for symbol,p in s.get('positions',{}).items():
            if p.get('status','OPEN') != 'OPEN':
                continue
            holdings.append(f"• {symbol}\n포착 {p.get('first_selected_date','기록 없음')} (일자) · 매수 {when(p.get('entry_at'))}\n{target(key,p)}")
        events = Path(root)/'paper_events.jsonl'
        if events.exists():
            with events.open() as stream:
                events = (json.loads(line) for line in stream if line.strip())
                rows = deque((r for r in events if r.get('type') in ('BUY','SELL')), maxlen=5)
            for r in reversed(rows):
                if r.get('type') in ('BUY','SELL'):
                    pnl = f" · 실현 {r['pnl_krw']:+,.0f}원" if 'pnl_krw' in r else ''
                    recent.append(f"{when(r['ts'])} {r.get('coin','')} {'매수' if r['type']=='BUY' else '매도'}{pnl}")
                    if len(recent) == 5: break
        if before != path.stat().st_mtime_ns:
            raise ValueError('STATE_CHANGED')
        return holdings, recent, ['매일 07:30 KST 자동 리밸런싱', '포착 시각 미저장분은 최초 선정 일자만 표시']
    db = readonly(state_path(root,key))
    try:
        s = json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        started = (s.get('review_segment') or {}).get('started_ms',s.get('started_ms',0)) if key == 'fast' else s.get('started_ms',0)
        for symbol,p in s['positions'].items():
            capture = p.get('decision_ms')
            if key in ('fast','bear','bollinger'):
                row = db.execute("SELECT decision_ms FROM fills WHERE symbol=? AND side='BUY' AND ts<=? ORDER BY id DESC LIMIT 1",(symbol,p['entry_ms'])).fetchone()
                capture = row[0] if row else None
            elif key == 'indicator':
                row = db.execute("SELECT payload FROM events WHERE symbol=? AND kind='BUY' ORDER BY id DESC LIMIT 1",(symbol,)).fetchone()
                capture = json.loads(row[0]).get('signal_ms') if row else None
            label = p.get('asset',symbol)
            line = f"• {label}\n포착 {when(capture)} · 매수/진입 {when(p.get('entry_ms'))}\n{target(key,p)}"
            if 'mark' in p and p.get('cost'):
                line += f"\n순평가 {(p['qty']*p['mark']/p['cost']-1)*100:+.2f}%"
                if p.get('uncertain') or not 0 <= now-p.get('mark_ms',0) <= 2000:
                    line += ' · 최근 관측값'
            holdings.append(line)
        if key in ('fast','bear','bollinger'):
            for r in db.execute('SELECT ts,symbol,side,pnl FROM fills WHERE ts>=? ORDER BY id DESC LIMIT 5',(started,)):
                recent.append(f"{when(r['ts'])} {r['symbol']} {'매수' if r['side']=='BUY' else '매도'}"+(f" · 실현 {r['pnl']:+,.0f}원" if r['side']=='SELL' else ''))
        elif key == 'indicator':
            for r in db.execute("SELECT ts,kind,symbol,payload FROM events WHERE kind IN ('BUY','SELL') ORDER BY id DESC LIMIT 5"):
                p = json.loads(r['payload'])
                recent.append(f"{when(p.get('fill_ms',r['ts']))} {r['symbol']} {'매수' if r['kind']=='BUY' else '매도'}"+(f" · 실현 {p['pnl']:+,.0f}원" if r['kind']=='SELL' else ''))
        else:
            for r in db.execute('SELECT ts,kind,pnl,payload FROM model_trades ORDER BY ts DESC LIMIT 5'):
                p = json.loads(r['payload'])
                recent.append(f"{when(r['ts'])} {p.get('asset','')} {r['kind']} 청산 · 실현 {r['pnl']:+,.0f}원")
        paused = s.get('entries_paused',False) or s.get('enabled') is False
        notes = ['신규 진입 '+('중지' if paused else '감시 중'),f"주문 대기 {len(s.get('pending',{}))}건"]
        if s.get('updated_ms') and now-s['updated_ms'] > 120000:
            notes.append('원장 갱신 지연 · 마지막 저장 상태')
        captures = []
        if key in ('fast','bear','bollinger'):
            captures = [(r['ts'],r['symbol']) for r in db.execute(
                'SELECT ts,symbol FROM signals WHERE ts>=? ORDER BY ts DESC LIMIT 3',(started,))]
        elif key == 'indicator':
            captures = [(r['ts'],r['symbol']) for r in db.execute(
                "SELECT ts,symbol FROM events WHERE kind='SIGNAL' ORDER BY id DESC LIMIT 3")]
        if captures:
            notes += ['최근 포착: '+', '.join(f'{symbol} {when(ts)}' for ts,symbol in captures)]
        if key == 'fast': notes.append('재시작 이후 매매만 표시')
        return holdings, recent, notes
    finally:
        db.close()


def view(root, now, section, key=None, offset=0):
    if section not in SECTIONS or (key is not None and key not in NAMES):
        raise ValueError('UNKNOWN_PAPER_VIEW')
    title = '모의투자현황 · 투자결과' if section=='results' and key is None else SECTIONS[section]
    lines = [title+' [PAPER]', '조회 '+clock(now)+' KST', '']
    if section == 'results':
        for k in ([key] if key else NAMES):
            lines += result_lines(root,k,now,compact=key is None)+['']
        lines += ['* FAST base 원금·성과는 재시작 기준',
                  '수익률: 보유평가 포함 · 승률: 비용 차감 후 전량 청산 기준']
        markup = keyboard(section,key)
        if key in ('vpd','fast','indicator','bollinger'):
            markup['inline_keyboard'].insert(0,[button('일별 결과',f'performance:{key}:0')])
        elif key:
            markup['inline_keyboard'].insert(0,[button('일별 결과·MDD', 'nav:fast_model_daily')])
        return '\n'.join(lines),markup
    if not key:
        lines.append('확인할 전략을 선택하세요.')
        if section == 'status': lines.append('보유종목 · 포착일시 · 목표수익률 · 최근 손익매매이력')
        else: lines.append('각 전략의 포착·매수·매도 조건을 설명합니다.')
        return '\n'.join(lines),keyboard(section)
    if section == 'guide':
        return '전략 설명 · '+guide(key),keyboard(section,key)
    lines.append(NAMES[key])
    try:
        holdings, recent, notes = status_data(root,key,now)
        offset = min(max(0,offset)//PAGE_SIZE*PAGE_SIZE,max(0,(len(holdings)-1)//PAGE_SIZE*PAGE_SIZE))
        lines += notes+['', f'보유 {len(holdings)}종목']
        lines += holdings[offset:offset+PAGE_SIZE] or ['보유 없음']
        lines += ['', '최근 손익매매이력 (최대 5건 · 매도는 비용 차감 실현손익)']+(recent or ['매매 기록 없음'])
        return '\n'.join(lines),keyboard(section,key,offset,len(holdings))
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError,IndexError):
        return '\n'.join(lines+['원장 준비 중 또는 갱신 중 · 현황 확인 대기']),keyboard(section,key)
