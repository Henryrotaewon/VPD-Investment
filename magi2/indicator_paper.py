"""Indicator acceleration PAPER ledger. Public books only; no live order client."""
import json
import math
from pathlib import Path
from magi2.fast_entry_limit import CaptureLimitLedger
from magi2.fast_target_service import TargetPaperService
from magi2.fast_paper import checked_book
from magi2.fast_wave_indicator import VERSION, evaluate
from magi2.fast_session import day, bounds

COHORT = 'indicator-paper-20260925-v1'
LABEL = 'MACD·RSI·거래량·Williams 가속도'


class IndicatorLedger(CaptureLimitLedger):
    # Keep the tested execution/accounting version; strategy/cohort are separate.
    entry_tick_limit_pct = 6
    book_only_profit = True
    indicator_strategy = True

    def __init__(self, path, now_ms):
        super().__init__(path, now_ms)
        with self.lock, self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS indicator_marks('
                            'minute INTEGER PRIMARY KEY, ts_ms INTEGER, date TEXT, payload TEXT)')
            self.db.execute('CREATE TABLE IF NOT EXISTS indicator_signals('
                            'id TEXT PRIMARY KEY, ts_ms INTEGER, venue TEXT, symbol TEXT, payload TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS indicator_marks_date ON indicator_marks(date,ts_ms)')

    def offer(self, *args, **kwargs):
        # Require the reproducible indicator + fresh flow decision path below.
        return False

    def offer_decision(self, result, capture_price, capture_ms, now_ms, generation):
        result = evaluate(result.get('observations', []), now_ms, flow=result.get('flow'))
        if (not result['paper_candidate'] or not 0 <= now_ms-capture_ms <= 3000
                or not math.isfinite(capture_price) or capture_price <= 0):
            return False
        last = result['latest']; venue, symbol = last['venue'], last['symbol']
        ident = f'{COHORT}:{venue}:{symbol}:{last["observed_ms"]}'
        with self.lock, self.db:
            state = self.control()
            if not state['enabled'] or state['generation'] != generation or last['observed_ms'] < state['resumed_ms']:
                return False
            if self._trade(ident): return False
            accepted = super().offer(ident, venue, symbol, now_ms, now_ms, strategy_version=VERSION)
            trade = self._trade(ident)
            if trade is None: return False
            trade.update(cohort=COHORT, indicator_evidence=result, indicator_score=result['score'],
                         capture_price=capture_price, capture_ms=capture_ms,
                         entry_price_policy=self.entry_price_policy, weak_count=0,
                         protection_activation_pct=6., protection_giveback_pp=2.,
                         max_hold_ms=3600000, peak_net_pct=None)
            self._save(trade)
            self.db.execute('INSERT OR IGNORE INTO indicator_signals VALUES(?,?,?,?,?)',
                            (ident, now_ms, venue, symbol, self._json(dict(result, accepted=accepted,
                             reason=trade.get('reason'), capture_price=capture_price, capture_ms=capture_ms))))
            return accepted

    def observe_holding(self, venue, symbol, result, now_ms):
        if not result.get('ready'): return
        point = result['latest']; stamp = point['observed_ms']
        if not 0 <= now_ms-stamp <= 90000: return
        with self.lock, self.db:
            for trade in self._active(venue):
                if trade['symbol'] != symbol or trade['status'] != 'OPEN': continue
                if stamp <= trade.get('last_indicator_ms', 0): continue
                gap = stamp-trade.get('last_indicator_ms', stamp)
                weak = (result['score'] < 30 and result['velocity_per_minute']['macd_hist_bps'] < 0
                        and result['velocity_per_minute']['rsi'] < 0)
                trade['weak_count'] = (trade.get('weak_count', 0)+1 if gap <= 180000 else 1) if weak else 0
                trade.update(last_indicator_ms=stamp, last_indicator_score=result['score'])
                self._event(trade, now_ms, 'INDICATOR_REVIEW', score=result['score'], weak=weak,
                            observed_ms=stamp, velocity=result['velocity_per_minute'])
                if trade['weak_count'] >= 2:
                    self._request_exit(trade, now_ms, 'INDICATOR_WEAKENED')
                else: self._save(trade)

    def check_stop(self, ident, book, now_ms):
        with self.lock, self.db:
            trade = self._trade(ident)
            if not trade or trade['status'] != 'OPEN': return False
            bids, _ = checked_book(book, now_ms)
            bid = bids[0][0]
            trade.update(last_bid=bid, mark_ms=book['received_ms'])
            net = (trade['remaining_qty']*bid*(1-trade['fee_bps']/10000)*
                   (1-trade['slippage_bps']/10000)/trade['remaining_cost']-1)*100
            peak = max(net, trade.get('peak_net_pct') if trade.get('peak_net_pct') is not None else net)
            trade['peak_net_pct'] = peak
            reason = ('STOP_LOSS_6' if bid <= trade['stop_price'] else
                      'TAKE_PROFIT_12' if bid >= trade['take_profit_price'] else
                      'PROFIT_PROTECTION' if peak >= trade['protection_activation_pct'] and
                      peak-net >= trade['protection_giveback_pp'] else
                      'MAX_HOLD_60M' if now_ms-trade['entry_ms'] >= trade['max_hold_ms'] else None)
            if reason:
                # All exits use current executable depth, fees and slippage, not an assumed limit fill.
                self._request_exit(trade, book['requested_ms'], reason)
                return True
            self._save(trade)
            return False

    def record_mark(self, now_ms):
        minute = now_ms//60000
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM indicator_marks WHERE minute=?', (minute,)).fetchone(): return
            snapshot = self.snapshot(now_ms)
            accounts = [{k:a[k] for k in ('venue','cash_krw','equity_krw','unknown_marks','pnl_krw',
                         'fees_krw','bought','closed','wins','skipped','funded_ms')} for a in snapshot['accounts']]
            total = sum(a['equity_krw'] for a in accounts) if all(a['equity_krw'] is not None for a in accounts) else None
            payload = dict(cohort=COHORT, strategy_version=VERSION, accounts=accounts, equity_krw=total,
                           position_count=sum(len(a['active']) for a in snapshot['accounts']))
            self.db.execute('INSERT INTO indicator_marks VALUES(?,?,?,?)',
                            (minute, now_ms, day(now_ms), self._json(payload)))

    def performance(self, date=None):
        with self.lock:
            where = 'WHERE date=?' if date else ''
            rows = self.db.execute('SELECT ts_ms,payload FROM indicator_marks '+where+' ORDER BY ts_ms',
                                   (date,) if date else ()).fetchall()
            points = [(ts,json.loads(p)) for ts,p in rows]
            valid = [(ts,p['equity_krw']) for ts,p in points if p['equity_krw'] is not None]
            peak = 4000000.; mdd = 0.
            for _,value in valid:
                peak = max(peak,value); mdd = min(mdd,(value/peak-1)*100)
            return dict(samples=len(points), valid_samples=len(valid), missing=len(points)-len(valid),
                        first=valid[0] if valid else None, last=valid[-1] if valid else None,
                        mdd_pct=mdd if valid else None)

    def daily_text(self, date, now_ms):
        from magi2.fast_paper_report import money, clock, NAMES
        start, end = bounds(date)
        perf = self.performance(date)
        lines = ['📅 FAST 지표 가속도 · 일별 매매평가', date+' 07:30~다음 날 07:30 KST',
                 '전략 '+VERSION+' · PAPER ONLY']
        if perf['last']:
            ts,equity = perf['last']
            lines += [f'마지막 유효 평가 {clock(ts)} · {money(equity)}',
                      f'최초원금 대비 {(equity/4000000-1)*100:+.2f}%']
            if end-ts > 90000: lines.append('마감 직전 평가 누락 · 위 금액은 일말 평가가 아닙니다.')
        else: lines.append('유효 평가 없음 · 수익률 계산 보류')
        with self.lock:
            for venue in ('upbit','bithumb','binance','kraken'):
                closed = [json.loads(x[0]) for x in self.db.execute(
                    "SELECT payload FROM paper_trades WHERE venue=? AND status='CLOSED' AND close_ms>=? AND close_ms<?",
                    (venue,start,end))]
                fills = self.db.execute('SELECT COALESCE(SUM(pnl_quote),0) FROM paper_fills '
                                        'WHERE venue=? AND ts_ms>=? AND ts_ms<?', (venue,start,end)).fetchone()[0]
                fx = self._account(venue)['fx_krw_per_quote']
                lines.append(f'{NAMES[venue]} · 완료 {len(closed)}건 · 양수 {sum(t["realized_quote"]>0 for t in closed)}건 · '
                             '기간 실현 '+money(fills*fx if fx else None, True))
        lines += [f'평가 관측 {perf["valid_samples"]}/{perf["samples"]} · 비용 반영 · 해외 환율 고정',
                  '과거 평가는 당시 저장한 값이며 현재 시세로 다시 계산하지 않습니다.']
        return '\n'.join(lines)


class IndicatorPaperService(TargetPaperService):
    ledger_class = IndicatorLedger

    def __init__(self, root, log, **kwargs):
        target = Path(root)/'indicator_paper'/COHORT
        target.mkdir(parents=True, exist_ok=True)
        super().__init__(target, log, **kwargs)

    def daily(self, send):
        ts = self.clock(); self.ledger.record_mark(ts)
        date = self.ledger.due_day(ts)
        if date: self.ledger.queue_daily(date, self.ledger.daily_text(date, ts), ts)
        pending = self.ledger.claim_daily(ts)
        if pending:
            date,text = pending; send(text); self.ledger.daily_sent(date)
            self.log('indicator_daily_sent date='+date)
