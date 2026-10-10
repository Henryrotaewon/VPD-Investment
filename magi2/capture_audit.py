"""Read-only strategy evidence and completed-session surge audits.

Only capture-audit/ is written. No trading engine is instantiated. Daily ranking
is retrospective opportunity coverage, NOT a strategy backtest or a buy signal.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import zlib
from zoneinfo import ZoneInfo
import requests

DAY = 86400000
MINUTE = 60000
KST = ZoneInfo('Asia/Seoul')
NAMES = {'vpd': 'VPD', 'fast': 'FAST', 'indicator': '지표가속',
         'bollinger': '더블볼린저', 'bear': 'FAST-BEAR', 'derivatives': 'FAST-DERIVATIVES'}
REASONS = {'STALE_FEED': '시세 최신성 부족', 'RELATIVE_VALUE_LOW': '비교 거래대금 부족',
           'BURST_LOW': '단기 거래대금 증가 부족', 'BUY_SHARE_LOW': '매수 비중 부족',
           'TRADE_COUNT_LOW': '체결 수 부족', 'OFI_NONPOSITIVE': '호가 수급 미충족',
           'HISTORY_INCOMPLETE': '비교 이력 부족', 'HISTORY_ZERO_VALUE': '비교 거래대금 0',
           'ALL_SLOTS_USED': '매수 슬롯 만석', 'NO_SLOT': '매수 슬롯 만석',
           'INSUFFICIENT_CASH': '가용현금 부족', 'HOLDING_OR_PENDING': '이미 보유·주문 중',
           'ENTRY_PRICE_CAP': '매수가 상한 초과', 'PROTECTION_ALREADY_BROKEN': '매수 전 보호선 이탈',
           'USER_STOPPED': '매수 중지', 'PROTECTION': '보호선 청산',
           'NO_NEW_HIGH_3M_NONPOSITIVE': '3분 고점 미갱신',
           'NET_SELL_20S_BELOW_VWAP': '순매도·VWAP 하회'}


def stamp(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()*1000)


def clock(value):
    return datetime.fromtimestamp(value/1000, KST).strftime('%m/%d %H:%M:%S')


def iso(value):
    return datetime.fromtimestamp(value/1000, timezone.utc).isoformat()


def closed_session(now):
    """09:10 KST is UTC 00:10. Never audit the forming trading session."""
    end = now//DAY*DAY
    if now-end < 10*MINUTE:
        end -= DAY
    return end-DAY, end


@contextmanager
def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    try:
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def ledger_path(root, strategy):
    paths = {'fast': 'fast-observe/paper-v1.sqlite3', 'bollinger': 'bollinger-paper/v1.sqlite3',
             'indicator': 'indicator_paper/indicator-hourly-20260926-v1/ledger.sqlite3',
             'bear': 'fast-models-v1/bear.sqlite3', 'derivatives': 'fast-models-v1/derivatives.sqlite3'}
    return Path(root)/paths[strategy]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def fast_trades(root, start, end, symbol=None):
    """Join each BUY to its own partial SELLs and diagnostic entry identity."""
    with readonly(ledger_path(root, 'fast')) as db:
        buys = db.execute("SELECT * FROM fills WHERE side='BUY' AND ts>=? AND ts<? "
                          + ('AND symbol=? ' if symbol else '')+'ORDER BY id DESC LIMIT 40',
                          (start, end, symbol) if symbol else (start, end)).fetchall()
        result = []
        for buy in buys:
            nxt = db.execute("SELECT id FROM fills WHERE symbol=? AND side='BUY' AND id>? ORDER BY id LIMIT 1",
                             (buy['symbol'], buy['id'])).fetchone()
            sells = db.execute("SELECT * FROM fills WHERE symbol=? AND side='SELL' AND id>? AND id<? AND ts<? ORDER BY id",
                               (buy['symbol'], buy['id'], nxt['id'] if nxt else 2**63-1, end)).fetchall()
            qty = sum(r['quantity'] for r in sells)
            complete = abs(qty-buy['quantity']) <= max(1e-8, buy['quantity']*1e-10)
            events = [];protection_changes=[]
            for r in db.execute("SELECT ts,kind,payload FROM events WHERE symbol=? AND ts>=? AND ts<? "
                                "AND kind IN ('ENTRY_DIAGNOSTIC','EXIT_DIAGNOSTIC','POST_EXIT_DIAGNOSTIC','PROTECTION_RAISED') ORDER BY id",
                                (buy['symbol'], buy['ts'], end)):
                p = json.loads(r['payload'])
                if r['kind']=='PROTECTION_RAISED' and r['ts']<(sells[-1]['ts']+1 if complete else end) and (not nxt or r['ts']<db.execute('SELECT ts FROM fills WHERE id=?',(nxt['id'],)).fetchone()[0]):
                    protection_changes.append(dict(ts=r['ts'],**p))
                if p.get('entry_ms') == buy['ts']:
                    events.append(dict(ts=r['ts'], kind=r['kind'], **p))
            entry = next((r for r in events if r['kind']=='ENTRY_DIAGNOSTIC'), {})
            exit_d = next((r for r in events if r['kind']=='EXIT_DIAGNOSTIC'), {})
            signal = db.execute('SELECT * FROM signals WHERE symbol=? AND ts=?',
                                (buy['symbol'], buy['decision_ms'])).fetchone()
            signal = json.loads(signal['payload']) if signal else {}
            cost = -buy['cash']
            pnl = sum(r['pnl'] for r in sells)
            result.append(dict(symbol=buy['symbol'], buy_id=buy['id'], entry_ms=buy['ts'],
                decision_ms=buy['decision_ms'], entry_price=buy['price'], cost=cost,
                complete=complete, sold_quantity=qty,
                exit_ms=sells[-1]['ts'] if complete else None,
                exit_price=sum(r['quantity']*r['price'] for r in sells)/qty if qty else None,
                net_pnl=pnl, net_return_pct=pnl/cost*100 if complete and cost else None,
                explicit_fees=buy['fee']+sum(r['fee'] for r in sells),
                holding_ms=sells[-1]['ts']-buy['ts'] if complete else None,
                reasons=sorted({r['reason'] for r in sells}),
                initial_stop=signal.get('protection', buy['price']*(1-entry['stop_distance_bps']/10000)
                    if entry.get('stop_distance_bps') is not None else None),
                signal=signal, entry_diagnostic=entry, exit_diagnostic=exit_d,
                protection_changes=protection_changes,
                post_exit=[r for r in events if r['kind']=='POST_EXIT_DIAGNOSTIC']))
        return result


class Evidence:
    """Bounded, compressed copy of diagnostics; never writes source ledgers."""
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root/'capture-audit'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.directory/'evidence.sqlite3', timeout=2)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS evidence(source TEXT,ident TEXT,ts INTEGER,symbol TEXT,kind TEXT,payload BLOB,
            PRIMARY KEY(source,ident));
          CREATE INDEX IF NOT EXISTS evidence_lookup ON evidence(source,symbol,ts);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS deliveries(day INTEGER PRIMARY KEY,status TEXT);
        ''')

    def add(self, source, ident, ts, symbol, kind, payload):
        self.db.execute('INSERT OR IGNORE INTO evidence VALUES(?,?,?,?,?,?)',
            (source,str(ident),ts,symbol,kind,zlib.compress(json.dumps(payload,ensure_ascii=False).encode())))

    def sync(self, now):
        sources = [('observer',self.root/'fast-observe/observe.sqlite3'),
                   *[(key,ledger_path(self.root,key)) for key in ('indicator','fast','bollinger','bear','derivatives')]]
        errors = {}
        for key,path in sources:
            try:
                row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
                cursor = int(row[0]) if row else 0
                with readonly(path) as src:
                    maximum=src.execute('SELECT max(id) FROM events').fetchone()[0] or 0
                    if maximum<cursor:cursor=0  # tolerate an explicitly reset/replaced source ledger
                    # Fast tick WINDOW rows are intentionally excluded. Archive decisions only.
                    kinds = ("('ASSESSMENT','CAPTURE','NOT_SELECTED','FAST_USER_STOPPED','FAST_USER_RESUMED')" if key=='observer'
                             else "('OBSERVATION','SIGNAL')" if key=='indicator'
                             else "('USER_PAUSED','USER_RESUMED','ENTRY_CANCELED','PRIOR_SIGNAL','RESTART')")
                    rows = src.execute(f'SELECT * FROM events WHERE id>? AND ts>=? AND kind IN {kinds} ORDER BY id',
                                       (cursor,now-3*DAY)).fetchall()
                    for r in rows:
                        p = json.loads(r['payload'])
                        if key=='observer' and r['kind']=='ASSESSMENT':
                            p = {k:p[k] for k in ('reasons','qualifies','fresh','enough','relative_value','buy_share','burst') if k in p}
                        if key=='indicator' and r['kind']=='OBSERVATION':
                            p = {k:p[k] for k in ('qualifies','boundary','day','history') if k in p}
                        self.add(key,str(r['ts'])+':'+str(r['id']),r['ts'],r['symbol'] if 'symbol' in r.keys() else p.get('asset',''),r['kind'],p)
                    if rows:
                        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,str(rows[-1]['id'])))
                    if key in ('bollinger','bear'):
                        for r in src.execute('SELECT * FROM assessments WHERE ts>=?', (now-3*DAY,)):
                            self.add(key,'assessment:'+str(r['ts'])+':'+r['symbol'],r['ts'],r['symbol'],
                                     'ASSESSMENT',json.loads(r['payload']))
            except (OSError,sqlite3.Error,ValueError,KeyError) as exc:
                errors[key] = type(exc).__name__
        try:
            # VPD snapshots are frozen at their actual as-of time, never relabelled as yesterday.
            snap = json.loads((self.root/'vpd_rebalance.json').read_text())
            ts = stamp(snap['asof'])
            for p in snap.get('all_rows',{}).values():
                self.add('vpd',str(ts)+p['market'],ts,p['market'],'SNAPSHOT',p)
        except (OSError,ValueError,KeyError):
            errors['vpd_snapshot'] = 'UNAVAILABLE'
        self.db.execute('DELETE FROM evidence WHERE ts<?',(now-3*DAY,))
        self.db.commit()
        return errors

    def rows(self, source, symbol, start, end):
        return [dict(ts=r[0],kind=r[1],payload=json.loads(zlib.decompress(r[2]))) for r in
                self.db.execute('SELECT ts,kind,payload FROM evidence WHERE source=? AND symbol=? AND ts>=? AND ts<? ORDER BY ts',
                                (source,symbol,start,end))]


class Public:
    """Dedicated background session, <=2 calls/sec; no private exchange APIs."""
    def __init__(self):
        self.http = requests.Session()
        self.next_request = 0.

    def get(self, path, params=None):
        for attempt in range(3):
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+.5
            r = self.http.get('https://api.upbit.com/v1/'+path,params=params,timeout=(3,8))
            if r.status_code==418:
                raise RuntimeError('PUBLIC_ACCESS_PAUSED')
            if r.status_code==429:
                time.sleep(2+attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError('PUBLIC_RATE_LIMIT')

    def ranking(self, start, end):
        markets = [x['market'] for x in self.get('market/all',{'is_details':'false'}) if x['market'].startswith('KRW-')]
        rows,missing = [],[]
        for market in markets:
            try:
                candles = self.get('candles/days',{'market':market,'to':iso(end),'count':2})
                target = next((x for x in candles if stamp(x['candle_date_time_utc']+'Z')==start),None)
                if not target or not target.get('prev_closing_price'):
                    missing.append(market);continue
                prior = float(target['prev_closing_price'])
                high,close = float(target['high_price']),float(target['trade_price'])
                if not all(math.isfinite(x) and x>0 for x in (prior,high,close)):
                    raise ValueError('INVALID_CANDLE')
                rows.append(dict(symbol=market,prior_close=prior,high=high,close=close,
                                 peak_pct=(high/prior-1)*100,close_pct=(close/prior-1)*100))
            except (requests.RequestException,ValueError,KeyError,StopIteration):
                missing.append(market)
        rows.sort(key=lambda r:(-r['peak_pct'],r['symbol']))
        return [r for r in rows if r['peak_pct']>=10][:5],dict(universe=len(markets),ready=len(rows),missing=missing,
            scope='CURRENTLY_LISTED_KRW; delisted markets unavailable; first-day/no-trade candles excluded')

    def crossing(self, symbol, start, end, prior):
        rows=[];cursor=end
        for _ in range(3):
            page=self.get('candles/minutes/5',{'market':symbol,'to':iso(cursor),'count':200})
            if not page:break
            rows += page
            cursor=min(stamp(r['candle_date_time_utc']+'Z') for r in page)
            if cursor<=start:break
        hits=[stamp(r['candle_date_time_utc']+'Z') for r in rows
              if start<=stamp(r['candle_date_time_utc']+'Z')<end and r['high_price']>=prior*1.1]
        return min(hits) if hits else None

    def history(self, symbol, start):
        return len(self.get('candles/days',{'market':symbol,'to':iso(start),'count':100}))


def strategy_evidence(root, archive, strategy, symbol, start, end, crossing, history):
    """Evidence labels distinguish observed decisions, inference, and missing history."""
    if strategy=='derivatives':
        from magi2.fast_models.market import ASSETS
        if symbol.removeprefix('KRW-') not in ASSETS:
            return dict(status='대상 외',detail='현선물 모델 고정 종목군에 없음')
        return dict(status='별도 모델',detail='현선물·하락추세 모델: 현물 급등 포착과 직접 비교 불가')
    rows=archive.rows(strategy,symbol,start,end)
    try:
        if strategy=='vpd':
            events=[]
            for line in (Path(root)/'paper_events.jsonl').read_text().splitlines():
                try:
                    r=json.loads(line)
                    if r.get('market')==symbol and start<=stamp(r['ts'])<end:events.append(r)
                except (ValueError,KeyError,TypeError):continue
            buys=[stamp(r['ts']) for r in events if r.get('type')=='BUY']
        else:
            with readonly(ledger_path(root,strategy)) as db:
                if strategy=='indicator':
                    all_rows=db.execute('SELECT ts,kind,payload FROM events WHERE symbol=? AND ts>=? AND ts<? ORDER BY id',
                                        (symbol,start,end)).fetchall()
                    rows=[dict(ts=r['ts'],kind=r['kind'],payload=json.loads(r['payload'])) for r in all_rows]
                    buys=[json.loads(r['payload']).get('fill_ms',r['ts']) for r in all_rows if r['kind']=='BUY']
                else:
                    buys=[r[0] for r in db.execute("SELECT ts FROM fills WHERE symbol=? AND side='BUY' AND ts>=? AND ts<? ORDER BY ts",(symbol,start,end))]
                    signals=db.execute('SELECT ts,status,reason,payload FROM signals WHERE symbol=? AND ts>=? AND ts<?',(symbol,start,end)).fetchall()
                    rows += [dict(ts=r['ts'],kind='SIGNAL',payload=dict(json.loads(r['payload']),status=r['status'],reason=r['reason'])) for r in signals]
        if buys:
            first=min(buys)
            timing=('10% 도달 전' if crossing is not None and first<crossing else
                    '10% 도달 후' if crossing is not None and first>=crossing+5*MINUTE else '도달 봉 내부·순서 미확인')
            return dict(status='매수',detail=clock(first)+' · '+timing,buys=buys)
    except (OSError,sqlite3.Error,ValueError):
        return dict(status='확인 대기',detail='매매 원장 조회 불가')
    if strategy=='indicator' and history is not None and history<100:
        return dict(status='미평가',detail=f'완성 일봉 {history}/100개 · 데이터 기준 제외',basis='RULE_RECONSTRUCTION')
    if strategy=='fast':
        with readonly(ledger_path(root,'fast')) as db:
            at=crossing if crossing is not None else start
            state=db.execute("SELECT ts,kind FROM events WHERE ts<=? AND kind IN ('USER_PAUSED','USER_RESUMED') ORDER BY id DESC LIMIT 1",(at,)).fetchone()
            if state and state['kind']=='USER_PAUSED':
                return dict(status='미매수',detail='급등 도달 시점 FAST 매수 중지',basis='RECORDED_PAUSE')
        captures=archive.rows('observer',symbol,start,end)
        actual=[r for r in captures if r['kind']=='CAPTURE']
        canceled=[r for r in rows if r['kind']=='ENTRY_CANCELED' or (r['kind']=='SIGNAL' and r['payload'].get('reason'))]
        if canceled:
            reason=canceled[-1]['payload'].get('reason','UNKNOWN')
            return dict(status='포착·미매수',detail=REASONS.get(reason,reason))
        if actual:
            return dict(status='포착·매수 없음',detail=clock(actual[0]['ts'])+' · 매수 제외 사유 미확인')
        sample=[r for r in captures if r['kind']=='ASSESSMENT' and crossing is not None and crossing-5*MINUTE<=r['ts']<=crossing]
        if sample:
            reasons=sample[-1]['payload'].get('reasons',[])
            return dict(status='미포착',detail='도달 전 관측: '+', '.join(REASONS.get(x,x) for x in reasons[:2]) if reasons else '단일 관측 통과 · 연속 확인 기록 부족')
    signals=[r for r in rows if r['kind']=='SIGNAL']
    if signals:
        p=signals[-1]['payload'];reason=p.get('decision') or p.get('reason') or p.get('status','UNKNOWN')
        return dict(status='포착·미매수',detail=REASONS.get(reason,reason))
    if strategy=='vpd':
        snaps=[r for r in rows if r['kind']=='SNAPSHOT' and (crossing is None or r['ts']<=crossing)]
        if snaps:
            p=snaps[-1]['payload']
            return dict(status='평가·미매수',detail=f"{clock(snaps[-1]['ts'])} {p.get('VPD','?')}점·{p.get('Rank','?')}위",basis='SNAPSHOT_NOT_EXECUTION_REASON')
    if strategy=='indicator':
        observations=[r for r in rows if r['kind']=='OBSERVATION']
        if observations:
            return dict(status='평가·매수 없음',detail=f"시간별 평가 {len(observations)}회 · 2회 연속 확정 신호 기록 없음")
    if strategy in ('bollinger','bear'):
        samples=[r for r in rows if r['kind']=='ASSESSMENT' and crossing is not None and crossing-10*MINUTE<=r['ts']<=crossing]
        if samples:
            p=samples[-1]['payload']
            detail=('돌파 '+str(bool(p.get('breakout')))+' · 거래대금 '+str(bool(p.get('volume_ok')))
                    if strategy=='bollinger' else 'RSI '+str(round(p.get('rsi',0),1)))
            return dict(status='평가·매수 없음',detail='도달 전 관측: '+detail+' · 확정 제외 사유는 미확인')
    return dict(status='미매수·사유 미확인',detail='관측 보존 범위 부족; 미포착으로 단정하지 않음')


def build_daily(root, archive, public, now):
    start,end=closed_session(now)
    ranked,coverage=public.ranking(start,end)
    for row in ranked:
        symbol=row['symbol']
        try:crossing=public.crossing(symbol,start,end,row['prior_close'])
        except (requests.RequestException,RuntimeError,ValueError):crossing=None
        try:history=public.history(symbol,start)
        except (requests.RequestException,RuntimeError,ValueError):history=None
        row.update(first_10pct_bucket=crossing,daily_history=history,strategies={})
        for strategy in NAMES:
            try:row['strategies'][strategy]=strategy_evidence(root,archive,strategy,symbol,start,end,crossing,history)
            except (sqlite3.Error,ValueError,KeyError,OSError):
                row['strategies'][strategy]=dict(status='확인 대기',detail='자료 조회 실패')
    return dict(version=1,start_ms=start,end_ms=end,generated_ms=now,coverage=coverage,rows=ranked,
                selection='전일 종가 대비 장중 고가 +10% 이상 중 상위 5개',
                caveat='사후 급등 종목 표본의 포착 점검; 승률·백테스트 아님. 도달 시각은 5분봉 구간. 과거 미보존 사유는 미확인.')


def daily_text(report):
    coverage=report['coverage']
    lines=['급등 포착 점검 [PAPER / 사후분석]',f"{clock(report['start_ms'])} ~ {clock(report['end_ms'])} KST",
           '조회 성공 시장 중 장중 +10% 이상 · 상승폭 상위 5개',
           f"조회 {coverage['ready']}/{coverage['universe']}시장 · 누락 {len(coverage['missing'])}"]
    for r in report['rows']:
        lines += ['',f"• {r['symbol']} 최고 {r['peak_pct']:+.1f}% / 마감 {r['close_pct']:+.1f}%"]
        for key in NAMES:
            s=r['strategies'][key]
            lines.append(f"{NAMES[key]}: {s['status']} · {s['detail']}")
    if not report['rows']:lines.append('조회 성공 시장에서 기준 충족 종목 없음')
    lines += ['', '기록 부족은 미포착과 구분 · 사후 표본이므로 전략 승률 아님',
              '현재 상장 시장 기준 · 도달 시각은 5분봉 구간']
    return '\n'.join(lines)


def trade_text(rows):
    lines=['FAST 매매 진단 [PAPER]','기존 원장·실제 관측 호가 기준 · 시각 KST']
    for r in rows[:5]:
        d=r.get('exit_diagnostic') or r.get('entry_diagnostic') or {}
        lines += ['',f"• {r['symbol']} 매수 {clock(r['entry_ms'])} · {r['entry_price']:g}원",
                  f"원가 {r['cost']:,.0f}원 · 초기 보호선 {r['initial_stop']:g}원" if r['initial_stop'] is not None else '초기 보호선 기록 없음']
        if r['complete']:
            lines += [f"매도 {clock(r['exit_ms'])} · {r['exit_price']:g}원 · 보유 {r['holding_ms']/1000:.1f}초",
                      f"순손익 {r['net_pnl']:+,.0f}원 ({r['net_return_pct']:+.2f}%) · "+'/'.join(REASONS.get(x,x) for x in r['reasons'])]
        else:lines.append('보유 중 또는 부분 청산 · 완료 수익률 미산출')
        for key,label,scale in [('confirmation_rise_bps','확인 대기 중 상승',.01),('entry_rise_bps','포착가 대비 체결 상승',.01),('stop_distance_bps','초기 보호선 거리',.01)]:
            if d.get(key) is not None:lines.append(f'{label} {d[key]*scale:.2f}%')
        if d.get('best_exit_net_pct') is not None:
            lines.append(f"보유 중 최고/최저 순평가 {d['best_exit_net_pct']:+.2f}% / {d['worst_exit_net_pct']:+.2f}%"+(' · 관측 공백' if not d.get('continuous') else ''))
        for p in r['post_exit']:
            lines.append(f"청산 {p['horizon_ms']//MINUTE}분 뒤 "+(f"보유 가정 순평가 {p['hold_net_pct']:+.2f}%" if p['status']=='OBSERVED_QUOTE_PROXY' else '호가 관측 부족'))
    if not rows:lines.append('최근 완료·진행 매매 자료 확인 대기')
    lines.append('\n청산 후 수치는 관측 호가로 계산한 참고값 · 실제 보유 수익률 아님')
    return '\n'.join(lines)


def view(root, section='daily', buy_id=None):
    directory=Path(root)/'capture-audit'
    try:
        data=json.loads((directory/('latest.json' if section=='daily' else 'fast_trades.json')).read_text())
        rows=data.get('rows',[])
        if buy_id is not None:rows=[r for r in rows if r.get('buy_id')==buy_id]
        message=daily_text(data) if section=='daily' else trade_text(rows)
    except (OSError,ValueError,KeyError):
        message='급등 포착 점검\n기록 수집 중 · 매일 09:10 KST 전일 점검'
    keyboard=[
        [{'text':'급등 포착 점검','callback_data':'nav:capture_audit'}, {'text':'FAST 매매 진단','callback_data':'nav:fast_trade_audit'}],
        [{'text':'↩️ 투자전략세부','callback_data':'nav:paper_status'}]]
    if section!='daily' and buy_id is None:
        keyboard[:0]=[[{'text':r['symbol']+' '+clock(r['entry_ms']),'callback_data':'audit_trade:'+str(r['buy_id'])}] for r in locals().get('rows',[])[:10]]
    return message,{'inline_keyboard':keyboard}


class Service:
    def __init__(self,root,log,send):
        self.root=Path(root);self.log=log;self.send=send;self.stop=threading.Event()

    def start(self):
        threading.Thread(target=self.worker,name='capture-audit',daemon=True).start()
        return self

    def trades(self,archive,now):
        rows=fast_trades(self.root,now-2*DAY,now)
        atomic_json(archive.directory/'fast_trades.json',dict(generated_ms=now,rows=rows))
        for row in rows:
            if not row['complete']:continue
            signature=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
            key='trade:'+str(row['buy_id'])
            old=archive.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
            if not old or old[0]!=signature:
                self.log('fast_trade_audit '+json.dumps(row,ensure_ascii=False))
                archive.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,signature))
        archive.db.commit()

    def deliver(self,archive,report):
        day=report['start_ms']
        # Claim before send: an uncertain Telegram response is never blindly replayed.
        with archive.db:
            claimed=archive.db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,'CLAIMED')",(day,)).rowcount
        if not claimed:return
        try:
            self.send(daily_text(report),view(self.root)[1])
            status='SENT'
        except Exception as exc:
            status='SEND_UNCERTAIN';self.log('capture_audit_delivery '+type(exc).__name__)
        with archive.db:archive.db.execute('UPDATE deliveries SET status=? WHERE day=?',(status,day))

    def worker(self):
        archive=Evidence(self.root);public=Public();last_attempt=0
        self.log('capture_audit_started daily=09:10KST session=09:00-09:00 threshold=10pct top=5 source_ledgers=read_only')
        while not self.stop.is_set():
            now=time.time_ns()//1000000
            try:
                errors=archive.sync(now)
                self.trades(archive,now)
                start,_=closed_session(now)
                path=archive.directory/(str(start)+'.json')
                if not path.exists() and now-last_attempt>=15*MINUTE:
                    last_attempt=now
                    report=build_daily(self.root,archive,public,now)
                    report['generated_ms']=time.time_ns()//1000000
                    report['evidence_errors']=errors
                    if report['coverage']['ready']==0:raise ValueError('NO_MARKET_COVERAGE')
                    atomic_json(path,report);atomic_json(archive.directory/'latest.json',report)
                    self.log('capture_audit_daily '+json.dumps(report,ensure_ascii=False))
                    self.deliver(archive,report)
                elif path.exists():
                    report=json.loads(path.read_text())
                    atomic_json(archive.directory/'latest.json',report)
                    self.deliver(archive,report)
            except Exception as exc:
                self.log('capture_audit_error '+type(exc).__name__)
            self.stop.wait(60)
