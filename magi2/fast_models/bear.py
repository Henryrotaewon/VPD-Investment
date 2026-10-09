"""B+C: oversold recovery followed by one full confirmation bar; fade exits."""
from collections import defaultdict
import json
from magi1.bollinger_paper import BollingerPaper, FIVE, warm_history
from magi1.fast_paper import Paper
from .metrics import Metrics

POLICY = dict(version='fast-bear-20261009-v1', initial=3000000., slots=10,
    fee=.0005, slip=.0005, entry_cap=.003, entry_wait_ms=10000,
    quote_age_ms=2000, no_new_high_ms=None, max_stop_distance=.015,
    entry='BEAR_REBOUND_CONFIRMED', protection='RECOVERY_LOW_MAX_1_5_PERCENT',
    flow_exit='FAILED_VWAP_RECOVERY', trail='AFTER_2_PERCENT_TO_1_PERCENT',
    gap_exit='FIRST_FRESH_BOOK_AFTER_GAP', same_day_reentry=False, retry_unfilled=False)


def rsi(closes):
    if len(closes)<15:return None
    delta=[b-a for a,b in zip(closes,closes[1:])]
    gain=sum(max(x,0) for x in delta[:14])/14
    loss=sum(max(-x,0) for x in delta[:14])/14
    for x in delta[14:]:
        gain=(gain*13+max(x,0))/14;loss=(loss*13+max(-x,0))/14
    return 100-100/(1+gain/loss) if loss else 100. if gain else 50.


def features(rows):
    rows=rows[-60:]
    if len(rows)<30 or any(b[0]-a[0]!=FIVE for a,b in zip(rows,rows[1:])):return None
    vol=sum(r[5] for r in rows[-12:])
    if vol<=0:return None
    closes=[r[4] for r in rows]
    return dict(rsi=rsi(closes),previous_rsi=rsi(closes[:-1]),close=closes[-1],
        low=rows[-1][3],vwap=sum(r[6] for r in rows[-12:])/vol,
        value=rows[-1][6],prior_value=sum(r[6] for r in rows[-7:-1])/6)


class BearPaper(BollingerPaper):
    def __init__(self,path,stamp):
        self.history=defaultdict(list);self.live=defaultdict(dict);self.active_since={}
        self.ready_since={};self.watch={};self.states={};self.loaded=set();self.needs_history=set()
        self.last_cycle=stamp//FIVE*FIVE;self.history_pages=0;self.history_updated=None
        self.last_processed={};self.flow=defaultdict(lambda:defaultdict(lambda:[0.,0.,0]))
        self.regime={'state':'UNAVAILABLE','ts':0};self.metrics=None
        Paper.__init__(self,path,stamp,POLICY)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS candles(symbol TEXT,bucket INTEGER,o REAL,h REAL,l REAL,c REAL,volume REAL,value REAL,PRIMARY KEY(symbol,bucket));
          CREATE TABLE IF NOT EXISTS assessments(symbol TEXT PRIMARY KEY,bucket INTEGER,ts INTEGER,payload TEXT);
        ''')
        for s,*row in self.db.execute('SELECT * FROM candles ORDER BY symbol,bucket'):self.history[s].append(row)
        self.metrics=Metrics(self.db,self.s['started_ms']);self.last_metric=0
        self.db.commit()

    def bought_today(self,symbol,ts):return Paper.bought_today(self,symbol,ts)

    def invalidate_candles(self,symbol,ts):
        self.flow.pop(symbol,None)
        super().invalidate_candles(symbol,ts)

    def trade_side(self,symbol,price,qty,stamp,side):
        bucket=stamp//FIVE*FIVE
        f=self.flow[symbol].setdefault(bucket,[0.,0.,0]);f[0]+=price*qty;f[1]+=price*qty if side=='BID' else 0;f[2]+=1

    def bearish(self,ts):return self.regime['state']=='DOWN' and 0<=ts-self.regime['ts']<360_000

    def assess(self,symbol,bucket,ts):
        rows=[r for r in self.history[symbol] if r[0]<=bucket]
        f=features(rows) if rows and rows[-1][0]==bucket else None
        flow=self.flow[symbol].get(bucket,[0,0,0])
        self.flow[symbol]={k:v for k,v in self.flow[symbol].items() if k>=bucket}
        if f is None:
            self.watch.pop(symbol,None);self.states[symbol]='INCOMPLETE_CANDLES';return
        f.update(buy_share=flow[1]/flow[0] if flow[0] else 0,trades=flow[2],regime=self.regime['state'])
        self.states[symbol]='READY' if self.bearish(ts) else 'REGIME_WAIT'
        p=self.s['positions'].get(symbol)
        if p:
            if f['close']<f['vwap'] and f['buy_share']<.4:
                self.request_exit(symbol,ts,'BEAR_FAILED_REBOUND')
            if ts-p['entry_ms']>=3_600_000:self.request_exit(symbol,ts,'BEAR_TIME_LIMIT')
        elif self.bearish(ts):
            armed=self.watch.pop(symbol,None)
            confirmed=bool(armed and bucket==armed['bucket']+FIVE and f['close']>max(f['vwap'],armed['vwap'])
                and f['low']>=armed['low'] and f['buy_share']>=.6 and f['trades']>=20)
            if confirmed:
                stop=max(armed['low']*.999,f['close']*(1-.014))
                self.signal(symbol,ts,f['close'],stop,dict(f,confirmation_of=armed['bucket']))
            elif f['previous_rsi']<=30<f['rsi'] and f['close']>f['vwap'] and f['buy_share']>=.6 and f['trades']>=20:
                self.watch[symbol]=dict(bucket=bucket,vwap=f['vwap'],low=f['low'])
        else:self.watch.pop(symbol,None)
        self.db.execute('INSERT OR REPLACE INTO assessments VALUES(?,?,?,?)',(symbol,bucket,ts,json.dumps(f)))

    def on_book(self,symbol,ts,book):
        if symbol in self.s['pending'] and not self.bearish(ts):self.cancel(symbol,ts,'BEAR_REGIME_LOST')
        p=self.s['positions'].get(symbol)
        if p:
            ret=book['bp']/p['entry_price']-1
            if ret>=.04:self.request_exit(symbol,ts,'BEAR_TAKE_PROFIT_4_PERCENT')
            elif ret>=.02:p['stop']=max(p['stop'],p['entry_price']*1.01)
        super().on_book(symbol,ts,book)

    def execute_exit(self,symbol,ts,book):
        p=self.s['positions'].get(symbol)
        if not p:return
        super().execute_exit(symbol,ts,book)
        if symbol not in self.s['positions'] and self.metrics:
            self.metrics.trade(symbol+':'+str(p['entry_ms']),ts,'REBOUND',p['total_pnl'],
                dict(symbol=symbol,entry_ms=p['entry_ms'],reason=p['exit']['reason']))
            self.db.commit()

    def tick(self,ts):
        super().tick(ts)
        if self.metrics and ts-self.last_metric>=10_000:
            self.last_metric=ts;r=super().report(ts)
            valid=not r['stale_marks'] and not r['uncertain_positions']
            self.metrics.mark(ts,r['equity'],valid);self.db.commit()

    def report(self,ts):
        return dict(super().report(ts),mode='FAST_BEAR_PAPER_ONLY',same_day_reentry=False,regime=self.regime,
                    mdd_pct=self.metrics.s['mdd'] if self.metrics else 0)
