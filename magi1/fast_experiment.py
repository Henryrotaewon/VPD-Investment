"""Paired prospective PAPER experiment; only confirmation count differs.

No partial candles, historical replay, shared cash, or real order APIs.
Each arm retains the observer's five active tracks and daily capture rules.
"""
from pathlib import Path
from magi1.fast_paper import Paper, POLICY, DAY


class Experiment:
    def __init__(self, directory, stamp):
        self.accounts = {}
        self.armed = {'control': set(), 'early': set()}
        self.first = {}
        for name, confirmations in [('control', 2), ('early', 1)]:
            policy = dict(POLICY, version='fast-entry-'+name+'-v1',
                          entry='FLOW_CONFIRMATION_'+str(confirmations))
            paper = Paper(str(Path(directory)/(name+'.sqlite3')), stamp, policy=policy)
            paper.db.execute('CREATE TABLE IF NOT EXISTS captures(symbol TEXT,day INTEGER,ts INTEGER,PRIMARY KEY(symbol,day))')
            paper.db.commit()
            self.accounts[name] = paper

    def evaluate(self, end, received, rows):
        stamp = max(end, received)
        candidates = {'control': [], 'early': []}
        for symbol, features, price, qualifies, old, bars in rows:
            if not qualifies:
                self.first.pop(symbol, None)
                for armed in self.armed.values():
                    armed.add(symbol)
                continue
            if old != (end-10000, True):
                self.first[symbol] = (end, price)
            first_ms, first_price = self.first.get(symbol, (end, price))
            evidence = dict(features, first_condition_ms=first_ms, first_condition_price=first_price,
                            confirmation_delay_ms=end-first_ms,
                            confirmation_rise_pct=(price/first_price-1)*100)
            for name in candidates:
                if name == 'early' or old == (end-10000, True):
                    candidates[name].append((symbol, evidence, price, bars))
        for name, choices in candidates.items():
            paper = self.accounts[name]
            choices.sort(key=lambda x: (-x[1]['relative_value'], -x[1]['net_buy'], x[0]))
            for symbol, features, price, bars in choices:
                captured = paper.db.execute('SELECT 1 FROM captures WHERE symbol=? AND day=?', (symbol, end//DAY)).fetchone()
                if captured and not (symbol in self.armed[name] and paper.can_signal(symbol, stamp)):
                    continue
                active = paper.db.execute('SELECT count(*) FROM captures WHERE ts>=?', (end-910000,)).fetchone()[0]
                if not captured and active >= 5:
                    continue
                if not captured:
                    paper.db.execute('INSERT INTO captures VALUES(?,?,?)', (symbol, end//DAY, end))
                recent = [b for b in bars if end-60000 <= b['t'] < end]
                lows = [b['l'] for b in recent if b.get('l') is not None]
                low = min(lows) if len(recent) == 6 and lows else None
                self.armed[name].discard(symbol)
                paper.event(stamp, 'EXPERIMENT_SIGNAL', symbol, dict(features, reference=price, protection=low))
                paper.signal(symbol, stamp, price, low, features)
                paper.db.commit()

    def report(self, stamp):
        result = {}
        for name, paper in self.accounts.items():
            report = paper.report(stamp)
            rows = paper.db.execute("SELECT reason,sum(pnl),count(*) FROM fills WHERE side='SELL' GROUP BY reason").fetchall()
            report['normal_exit_pnl'] = sum(pnl for reason,pnl,n in rows if not reason.startswith('DATA_GAP_'))
            report['gap_exit_pnl'] = sum(pnl for reason,pnl,n in rows if reason.startswith('DATA_GAP_'))
            report['gap_sell_fills'] = sum(n for reason,pnl,n in rows if reason.startswith('DATA_GAP_'))
            report['captures'] = paper.db.execute('SELECT count(*) FROM captures').fetchone()[0]
            result[name] = report
        return result
