"""Forward-only FAST-BASE alerts to the existing authorized MAGI chat.

Separate sender thread: no Telegram wait in a price observer. Durable local
claims suppress duplicate capture delivery; ambiguous failures are not retried
because Telegram sendMessage has no idempotency key.
"""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

VENUES = {'upbit':'업비트','bithumb':'빗썸','binance':'바이낸스','kraken':'크라켄'}


def capture_text(row, venue, account_path):
    ident,ts,symbol,price,payload = row
    evidence=json.loads(payload); status='모의매수 여부 확인 대기';risk='TP/SL: BTC 국면 확인 대기'
    unit={'upbit':'원','bithumb':'원','binance':'USDT','kraken':'USD'}[venue]
    try:
        with closing(sqlite3.connect(account_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as db:
            state_row=db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            state=json.loads(state_row[0]) if state_row else {}
            p=state.get('positions',{}).get(symbol)
            signal=db.execute('SELECT status,reason,payload,ts FROM signals WHERE symbol=? AND day=?',
                              (symbol,ts//86400000)).fetchone()
            if p and p['entry_ms']>=ts:
                status=f"모의매수 체결 · {p['entry_price']:,.6g}{unit}"
                r=p.get('risk') or {}
            elif signal and signal[3]>=ts:
                from magi2.fast_flow_paper_report import reason_text
                status=('모의매수 대기 · 가격·잔량 조건 충족 시 체결' if signal[0]=='PENDING' else
                        '매수 보류 · '+reason_text(signal[1]) if signal[0]=='SKIPPED' else
                        '모의매수 체결 이력 있음 · 현재 보유 없음')
                f=json.loads(signal[2]);r={'tp':f.get('take_profit'),'sl':f.get('stop_loss')}
            elif p:
                status='기존 보유 종목 · 추가 매수 없음';r=p.get('risk') or {}
            else:
                r={}
            if r.get('tp') is not None and r.get('sl') is not None:
                risk=f"TP +{r['tp']*100:g}% / SL −{r['sl']*100:g}% (순손익) · 최대 24시간"
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        pass
    clock=datetime.fromtimestamp(ts/1000,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')
    def num(key,scale=1,suffix=''):
        value=evidence.get(key)
        return f'{value*scale:.1f}{suffix}' if isinstance(value,(int,float)) else '확인 대기'
    return ('⚡ FAST-BASE 포착 [PAPER]\n'
            f"{VENUES[venue]} · {symbol}\n{clock} KST · 포착가 {price:,.6g}{unit}\n"
            f"거래대금 {num('relative_value',suffix='배')} · 단기 급증 {num('burst',suffix='배')} · 매수 비중 {num('buy_share',100,'%')}\n"
            f'{status}\n{risk}\n포착 알림이며 실제 주문은 없습니다.')


class Service:
    def __init__(self, root, log, send, clock=None):
        self.root=Path(root);self.log=log;self.send=send
        self.clock=clock or (lambda:time.time_ns()//1000000)
        self.path=self.root/'fast-observe'/'capture-alerts.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stopped=threading.Event()
        with closing(self.connect()) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value INTEGER);
                CREATE TABLE IF NOT EXISTS delivery(venue TEXT,capture_id INTEGER,ts INTEGER,status TEXT,
                PRIMARY KEY(venue,capture_id));''')
            db.execute("INSERT OR IGNORE INTO meta VALUES('activated_ms',?)",(self.clock(),))
            db.commit()
        self.log('fast_capture_alerts_ready forward_only=true recipient=existing_magi_chat')

    def connect(self):
        return sqlite3.connect(self.path,timeout=1)

    def poll(self):
        now=self.clock()
        with closing(self.connect()) as delivery:
            activation=delivery.execute("SELECT value FROM meta WHERE key='activated_ms'").fetchone()[0]
            for venue in VENUES:
                directory=self.root/'fast-observe'
                if venue!='upbit':directory=directory/'venues'/venue
                try:
                    with closing(sqlite3.connect((directory/'observe.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as db:
                        rows=db.execute('SELECT id,ts,symbol,price,payload FROM captures WHERE ts>=? AND ts<=? ORDER BY id LIMIT 50',
                                        (max(activation,now-120000),now)).fetchall()
                except sqlite3.Error:
                    continue
                for row in rows:
                    claimed=delivery.execute('INSERT OR IGNORE INTO delivery VALUES(?,?,?,?)',
                                             (venue,row[0],now,'CLAIMED')).rowcount
                    delivery.commit()
                    if not claimed:continue
                    try:
                        self.send(capture_text(row,venue,directory/'paper-v1.sqlite3'),
                                  {'inline_keyboard':[[{'text':'FAST 전략세부','callback_data':'paper:status:fast:0'}]]})
                        status='SENT'
                    except Exception as exc:
                        status='DELIVERY_UNCONFIRMED'
                        self.log('fast_capture_alert_delivery_unconfirmed venue='+venue+' type='+type(exc).__name__)
                    delivery.execute('UPDATE delivery SET status=? WHERE venue=? AND capture_id=?',(status,venue,row[0]))
                    delivery.commit()
            delivery.execute('DELETE FROM delivery WHERE ts<?',(now-7*86400000,))
            delivery.commit()

    def run(self):
        while not self.stopped.is_set():
            try:self.poll()
            except Exception as exc:self.log('fast_capture_alert_error '+type(exc).__name__)
            self.stopped.wait(2)

    def start(self):
        threading.Thread(target=self.run,name='fast-capture-alerts',daemon=True).start()
        return self
