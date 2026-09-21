"""TOP5 rotation state. Rank snapshots commit before any simulated order."""
import hashlib
import json
from magi2.fast_target_paper import TargetLedger
from magi2.fast_tick_paper import TickLedger
from magi2.fast_paper import checked_book
from magi2.fast_tick_rules import dec, floor_qty, valid_size

VERSION = 'fast-daily-top5-v2'
DAY = 86400000
INTERVAL = 300000


def rank_day(ts):
    return ts // DAY * DAY  # UTC midnight = 09:00 KST


def rank_slot(ts):
    return (ts - 60000) // INTERVAL * INTERVAL + 60000


class RankLedger(TargetLedger):
    entry_participation = 1.0
    book_only_profit = True
    # User-selected threshold: reject exactly 6% as well as larger ticks.
    entry_tick_limit_pct = 6

    def __init__(self, path, now_ms):
        super().__init__(path, now_ms)
        with self.lock, self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS fast_rank_snapshots('
                'venue TEXT, slot INTEGER, payload TEXT NOT NULL, PRIMARY KEY(venue,slot))')
            self.db.execute('CREATE TABLE IF NOT EXISTS fast_rank_baselines('
                'venue TEXT, day INTEGER, symbol TEXT, price REAL, PRIMARY KEY(venue,day,symbol))')

    def baseline(self, venue, day, symbol):
        with self.lock:
            row=self.db.execute('SELECT price FROM fast_rank_baselines WHERE venue=? AND day=? AND symbol=?',
                                (venue,day,symbol)).fetchone()
            return (row is not None, row[0] if row else None)

    def configure_launch(self, at_ms, now_ms):
        with self.lock,self.db:
            exists=self.db.execute("SELECT 1 FROM paper_meta WHERE key='rank_launch'").fetchone()
            if exists:return False
            state=self._control(False,now_ms)
            self.db.execute('INSERT INTO paper_meta VALUES(?,?)',('rank_launch',self._json(
                dict(at_ms=at_ms,pending=True,generation=state['generation']))))
            return True

    def launch(self):
        with self.lock:
            row=self.db.execute("SELECT value FROM paper_meta WHERE key='rank_launch'").fetchone()
            return json.loads(row[0]) if row else None

    def activate_due(self, now_ms):
        with self.lock,self.db:
            launch=self.launch()
            if not launch or not launch['pending'] or now_ms<launch['at_ms']:return False
            state=self.control();launch['pending']=False
            # A subsequent user stop/start always takes precedence over the timer.
            activated=state['generation']==launch['generation']
            if activated:self._control(True,launch['at_ms'])
            launch.update(activated_ms=now_ms if activated else None)
            self.db.execute("UPDATE paper_meta SET value=? WHERE key='rank_launch'",(self._json(launch),))
            return activated

    def prewarming(self):
        launch=self.launch();state=self.control()
        return bool(launch and launch['pending'] and launch['generation']==state['generation'])

    def save_baseline(self, venue, day, symbol, price):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO fast_rank_baselines VALUES(?,?,?,?)',(venue,day,symbol,price))
            self.db.execute('DELETE FROM fast_rank_baselines WHERE day<?',(day-DAY,))

    def latest_rank(self, venue):
        with self.lock:
            row=self.db.execute('SELECT payload FROM fast_rank_snapshots WHERE venue=? ORDER BY slot DESC LIMIT 1',
                                (venue,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_rank(self, venue, slot, rows, now_ms, generation):
        with self.lock, self.db:
            state=self.control()
            if (not state['enabled'] or generation!=state['generation'] or slot<state['resumed_ms']
                    or rank_slot(now_ms)!=slot or not 0<=now_ms-slot<=30000
                    or rank_day(slot)!=rank_day(now_ms)):
                return False
            previous=self.latest_rank(venue)
            if previous and previous['slot']>=slot:return False
            old={r['symbol']:r for r in previous['rows']} if previous and previous['generation']==generation else {}
            entries=[]
            for row in rows:
                prior=old.get(row['symbol'])
                entries.append(dict(row, entry_slot=prior['entry_slot'] if prior else slot,
                                    consumed=prior.get('consumed',False) if prior else False))
            snapshot=dict(venue=venue,slot=slot,day=rank_day(slot),saved_ms=now_ms,
                          generation=generation,rows=entries,
                          basis=rows[0].get('basis','PREVIOUS_DAY_CLOSE') if rows else 'PREVIOUS_DAY_CLOSE')
            self.db.execute('INSERT INTO fast_rank_snapshots VALUES(?,?,?)',(venue,slot,self._json(snapshot)))
            self.db.execute('DELETE FROM fast_rank_snapshots WHERE slot<?',(slot-7*DAY,))
            return True

    def offer_rank(self, venue, now_ms):
        accepted=[]
        with self.lock, self.db:
            snapshot=self.latest_rank(venue);control=self.control()
            if (not snapshot or not control['enabled'] or snapshot['generation']!=control['generation']
                    or not 0<=now_ms-snapshot['saved_ms']<=10000):return accepted
            for row in snapshot['rows']:
                if row['consumed']:continue
                active=self._active(venue)
                if any(t['symbol']==row['symbol'] for t in active):
                    row['consumed']=True;continue
                if len(active)>=5:break
                a=self._account(venue)
                if a['funded_ms'] is None:break
                reserved=sum(t.get('session_cash',0) for t in active)
                if a['cash_quote']-reserved<=1e-8:break
                ident=f'top5:{venue}:{row["symbol"]}:{row["entry_slot"]}:{control["generation"]}'
                if self._trade(ident):
                    row['consumed']=True;continue
                ok=TickLedger.offer(self,ident,venue,row['symbol'],now_ms,now_ms,
                                    strategy_version=VERSION,allow_partial_budget=True)
                t=self._trade(ident)
                t.update(deadline_ms=None,take_profit_pct=12.,stop_loss_pct=-6.,rank_slot=snapshot['slot'],
                         previous_close=row.get('previous_close'),rise_pct=row['rise_pct'],
                         baseline_price=row.get('baseline_price',row.get('previous_close')),
                         rank_basis=row.get('basis','PREVIOUS_DAY_CLOSE'))
                self._save(t);row['consumed']=True
                if ok:accepted.append(ident)
            self.db.execute('UPDATE fast_rank_snapshots SET payload=? WHERE venue=? AND slot=?',
                            (self._json(snapshot),venue,snapshot['slot']))
        return accepted

    def offer(self, *args, **kwargs):
        # All entries require a durable rank snapshot and membership episode.
        return False

    def check_stop(self, ident, book, now_ms):
        with self.lock, self.db:
            t=self._trade(ident)
            if not t or t['status']!='OPEN':return False
            bids,_=checked_book(book,now_ms)
            t.update(last_bid=bids[0][0],mark_ms=book['received_ms'])
            # Fixed +12% limit: executable public bid depth, never synthetic tape.
            order=t.get('order')
            if order and now_ms>=order['ready_ms'] and bids[0][0]>=order['price']:
                key=hashlib.sha256(self._json(bids).encode()).hexdigest()
                if key!=t.get('profit_book_hash'):
                    t['profit_book_hash']=key
                    for price,size in bids:
                        if price<order['price']:break
                        qty=floor_qty(min(order['remaining'],size),t['rules']['step'])
                        if qty<=0 or not valid_size(price,qty,t['rules']):continue
                        self._after_limit_fill(t,order,qty,price,now_ms,price,t['rules'],'TAKER',
                                               dict(book_received_ms=book['received_ms']))
                        if t['status']=='CLOSED':break
                    self._save(t)
                return t['status']=='CLOSED'
            snapshot=self.latest_rank(t['venue']);state=self.control()
            fresh=(snapshot and state['enabled'] and snapshot['generation']==state['generation']
                   and rank_day(now_ms)==snapshot['day'] and 0<=now_ms-snapshot['saved_ms']<=30000
                   and book['requested_ms']>=snapshot['saved_ms'])
            if fresh and t.get('stop_checked_slot')!=snapshot['slot']:
                t['stop_checked_slot']=snapshot['slot']
                outside=t['symbol'] not in {r['symbol'] for r in snapshot['rows']}
                if outside and dec(bids[0][0])<=dec(t['stop_price']):
                    self._request_exit(t,book['requested_ms'],'TOP5_EXIT_AND_STOP_6')
                    return True
            self._save(t)
            return False
