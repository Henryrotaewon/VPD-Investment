"""Public spot market context. Observation only; never authorizes orders.

Returns are decimal fractions. Day = 00:00 UTC / 09:00 KST. Classifier
thresholds are versioned research hypotheses, not calibrated probabilities.
"""
import asyncio
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import time
from datetime import datetime, timezone, timedelta

import requests

SCHEMA = 'magi1-market-context-v1'
POLICY = 'regime-observation-v1'
INTERVAL = 300
TTL = 600
DAY = 86400
EXCLUDED = {'USDT','USDC','USDE','USDS','DAI','TUSD','FDUSD','USDP','USD1',
            'USDD','PYUSD','RLUSD','BUSD','EUR','EURC','EURI','PAXG','XAUT'}
MAJORS = {'BTC', 'ETH'}
FRED = {'SP500':'S&P500', 'NASDAQCOM':'NASDAQ', 'DGS10':'미국10년금리',
        'DTWEXBGS':'광의달러', 'DCOILWTICO':'WTI'}


def number(x, positive=False):
    x = float(x)
    if not math.isfinite(x) or (positive and x <= 0):
        raise ValueError('INVALID_NUMBER')
    return x


def stamp(iso):
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


def direction(value, band):
    return 'UP' if value > band else 'DOWN' if value < -band else 'FLAT'


def series_metrics(rows, interval, now, count):
    end = int(now//interval)*interval
    parsed = {}
    for t, close in rows:
        t = int(t)
        if t % interval or t > now+30:
            raise ValueError('INVALID_CANDLE_TIME')
        if end-count*interval <= t < end:
            if t in parsed:
                raise ValueError('DUPLICATE_CANDLE')
            parsed[t] = number(close, True)
    expected = list(range(end-count*interval, end, interval))
    if sorted(parsed) != expected:
        raise ValueError('MISSING_COMPLETED_CANDLES')
    return [parsed[t] for t in expected], end


def asset_metrics(hourly, daily, quote, now):
    h, h_end = series_metrics(hourly, 3600, now, 25)
    d, d_end = series_metrics(daily, DAY, now, 31)
    changes = [math.log(b/a) for a,b in zip(h,h[1:])]
    return dict(day_return=quote['return'], return_1h=h[-1]/h[-2]-1,
                return_4h=h[-1]/h[-5]-1, return_24h=h[-1]/h[-25]-1,
                return_7d=d[-1]/d[-8]-1, return_30d=d[-1]/d[-31]-1,
                realized_vol_24h=statistics.stdev(changes)*math.sqrt(24),
                day_drawdown=quote['price']/quote['high']-1,
                hourly_close_ts_ms=h_end*1000, daily_close_ts_ms=d_end*1000,
                quote_ts_ms=quote['ts_ms'])


def classify_venue(quotes, expected, btc, eth):
    valid = len(quotes)
    alts = [q for a,q in quotes.items() if a not in MAJORS]
    coverage = valid/expected if expected else 0
    if coverage < .8 or len(alts) < 20 or not MAJORS.issubset(quotes):
        raise ValueError('INSUFFICIENT_MARKET_COVERAGE')
    total = sum(q['turnover'] for q in quotes.values())
    if total <= 0:
        raise ValueError('NO_TURNOVER')
    breadth = sum(q['return'] > 0 for q in alts)/len(alts)
    relative = sum(q['return'] > btc['day_return'] for q in alts)/len(alts)
    median = statistics.median(q['return'] for q in alts)
    major_share = sum(quotes[a]['turnover'] for a in MAJORS)/total
    participation = ('ALT_EXPANSION' if breadth >= .6 and relative >= .5 and median > .002
                     else 'MAJOR_LED' if major_share >= .35 or (
                         btc['day_return'] > 0 and relative < .4 and median < btc['day_return'])
                     else 'BROAD_WEAKNESS' if breadth <= .35 and median < -.002
                     else 'MIXED')
    votes = [direction(btc['day_return'], .003), direction(btc['return_1h'], .0025),
             direction(btc['return_4h'], .005)]
    trend = 'UP' if votes.count('UP') >= 2 else 'DOWN' if votes.count('DOWN') >= 2 else 'MIXED'
    risk = 'HIGH' if btc['realized_vol_24h'] >= .04 or btc['day_drawdown'] <= -.03 else 'NORMAL'
    return dict(status='OK', direction=trend, participation=participation, risk=risk,
                medium_direction=direction(btc['return_7d'], .01),
                today_direction=direction(btc['day_return'], .003),
                short_direction=direction(btc['return_4h'], .005),
                btc=btc, eth=eth,
                breadth=dict(eligible=expected, valid=valid, alt_count=len(alts), coverage=coverage,
                             advancing_ratio=breadth, outperform_btc_ratio=relative,
                             median_return=median, major_turnover_share=major_share,
                             turnover_basis='UTC_DAY', return_basis='UTC_DAY_OPEN'),
                reasons=[f'BTC_DAY={btc["day_return"]:.6f}', f'BTC_4H={btc["return_4h"]:.6f}',
                         f'ALT_ADVANCING={breadth:.4f}', f'ALT_OUTPERFORM={relative:.4f}',
                         f'MAJOR_TURNOVER_SHARE={major_share:.4f}'])


def confirm(current, previous, now):
    """Two separate >=4 minute observations; never confirm across a stale gap."""
    recent = previous and 240 <= now-previous.get('observed_ts_ms',0)/1000 <= TTL
    for venue, row in current['venues'].items():
        if row['status'] != 'OK':
            continue
        raw = row['direction']+'/'+row['participation']
        prior = (previous or {}).get('venues',{}).get(venue,{}) if recent else {}
        streak = prior.get('confirmation_count',0)+1 if prior.get('observed_state')==raw else 1
        row.update(observed_state=raw, confirmation_count=min(streak,2),
                   confirmation='CONFIRMED' if streak>=2 else 'PENDING')
    return current


class Collector:
    def __init__(self, get=None):
        self.get = get or requests.get
        self.cache = {}

    def fetch(self, url, params=None, cache_seconds=0, now=None, text=False):
        now = time.time() if now is None else now
        key = url+json.dumps(params,sort_keys=True)
        if key in self.cache and now-self.cache[key][0] < cache_seconds:
            return self.cache[key][1]
        r = self.get(url, params=params, timeout=(3,8))
        r.raise_for_status()
        data = r.text if text else r.json()
        if cache_seconds:
            self.cache[key] = (now,data)
        return data

    def upbit(self, now):
        base = 'https://api.upbit.com/v1'
        markets = self.fetch(base+'/market/all', {'isDetails':'true'}, 3600, now)
        symbols = {x['market']:x['market'][4:] for x in markets
                   if x['market'].startswith('KRW-') and x['market'][4:] not in EXCLUDED
                   and x.get('market_warning','NONE')=='NONE'
                   and not (x.get('market_event') or {}).get('warning',False)}
        quotes = {}
        names = sorted(symbols)
        for i in range(0,len(names),100):
            rows = self.fetch(base+'/ticker', {'markets':','.join(names[i:i+100])})
            for x in rows:
                try:
                    a = symbols[x['market']]
                    ts = number(x['trade_timestamp'])
                    if not -30000 <= now*1000-ts <= 300000:
                        continue
                    p, op, hi = [number(x[k],True) for k in ('trade_price','opening_price','high_price')]
                    vol = number(x['acc_trade_price'])
                    if vol < 0 or hi < p: continue
                    quotes[a] = dict(price=p,return_=p/op-1, high=hi,turnover=vol,ts_ms=ts)
                    quotes[a]['return'] = quotes[a].pop('return_')
                except (KeyError,ValueError,TypeError):
                    continue
        assets = {}
        for asset in ('BTC','ETH'):
            hr = self.fetch(base+'/candles/minutes/60',{'market':'KRW-'+asset,'count':30})
            dy = self.fetch(base+'/candles/days',{'market':'KRW-'+asset,'count':40})
            h = [(stamp(x['candle_date_time_utc']),x['trade_price']) for x in hr]
            d = [(stamp(x['candle_date_time_utc']),x['trade_price']) for x in dy]
            assets[asset] = asset_metrics(h,d,quotes[asset],now)
        return classify_venue(quotes,len(symbols),assets['BTC'],assets['ETH'])

    def binance(self, now):
        base = 'https://api.binance.com/api/v3'
        info = self.fetch(base+'/exchangeInfo',cache_seconds=3600,now=now)
        symbols = {x['symbol']:x['baseAsset'] for x in info['symbols']
                   if x['quoteAsset']=='USDT' and x['status']=='TRADING'
                   and x.get('isSpotTradingAllowed',False) and x['baseAsset'] not in EXCLUDED
                   and x['baseAsset'] not in {'BTCUP','BTCDOWN','ETHUP','ETHDOWN','BNBUP','BNBDOWN'}}
        rows = self.fetch(base+'/ticker/tradingDay',{'type':'MINI','timeZone':'0'})
        quotes = {}
        start = int(now//DAY)*DAY*1000
        for x in rows:
            if x.get('symbol') not in symbols: continue
            try:
                ts = number(x['closeTime'])
                if not -30000 <= now*1000-ts <= 300000 or int(x['openTime']) != start: continue
                p,op,hi = [number(x[k],True) for k in ('lastPrice','openPrice','highPrice')]
                vol = number(x['quoteVolume'])
                if vol<=0 or hi<p: continue
                quotes[symbols[x['symbol']]] = dict(price=p,high=hi,turnover=vol,ts_ms=ts)
                quotes[symbols[x['symbol']]]['return'] = p/op-1
            except (KeyError,TypeError,ValueError): continue
        assets = {}
        for asset in ('BTC','ETH'):
            h = self.fetch(base+'/klines',{'symbol':asset+'USDT','interval':'1h','limit':30})
            d = self.fetch(base+'/klines',{'symbol':asset+'USDT','interval':'1d','limit':40})
            assets[asset] = asset_metrics([(x[0]//1000,x[4]) for x in h],
                                         [(x[0]//1000,x[4]) for x in d],quotes[asset],now)
        return classify_venue(quotes,len(symbols),assets['BTC'],assets['ETH'])

    def macro(self, now):
        # Daily economic data remain separate from live crypto directions.
        result = {}
        today = datetime.fromtimestamp(now,timezone.utc).date()
        for ident,label in FRED.items():
            params = {'id':ident,'cosd':(today-timedelta(days=30)).isoformat()}
            url = 'https://fred.stlouisfed.org/graph/fredgraph.csv'
            cache_key = url+json.dumps(params,sort_keys=True)
            try:
                raw = self.fetch(url,params,21600,now,text=True)
                observations = []
                for row in csv.DictReader(io.StringIO(raw)):
                    if row.get(ident) in (None,'','.'): continue
                    date = datetime.fromisoformat(row.get('observation_date') or row['DATE']).date()
                    if date >= today: continue  # Do not treat an unfinished dated value as published history.
                    observations.append((date,number(row[ident])))
                observations.sort()
                if len(observations)<2: raise ValueError('NO_HISTORY')
                (date,value),(_,prev) = observations[-1],observations[-2]
                age = (today-date).days
                result[ident] = dict(label=label,status='DELAYED' if age<=7 else 'STALE',
                                     observation_date=date.isoformat(), value=value,
                                     change=value-prev if ident=='DGS10' else value/prev-1,
                                     change_unit='PERCENTAGE_POINTS' if ident=='DGS10' else 'FRACTION',
                                     source='FRED',age_calendar_days=age,
                                     received_ts_ms=int(self.cache.get(cache_key,(now,None))[0]*1000))
            except (requests.RequestException,ValueError,KeyError,TypeError,ZeroDivisionError):
                self.cache.pop(cache_key,None)
                result[ident] = dict(label=label,status='UNAVAILABLE',source='FRED')
        return result

    def collect(self, now=None, previous=None):
        now = time.time() if now is None else now
        venues = {}
        for venue in ('upbit','binance'):
            try:
                venues[venue] = getattr(self,venue)(now)
            except (requests.RequestException,ValueError,KeyError,TypeError,IndexError,AttributeError) as exc:
                reason = str(exc) if type(exc) is ValueError else type(exc).__name__
                venues[venue] = dict(status='UNAVAILABLE',reason=reason[:80])
        payload = dict(schema_version=SCHEMA,policy_version=POLICY,producer='MAGI1',
                       mode='OBSERVATION_ONLY',execution_eligible=False,
                       observed_ts_ms=int(now*1000),expires_ts_ms=int((now+TTL)*1000),
                       day_start_ts_ms=int(now//DAY)*DAY*1000,day_basis='00:00 UTC / 09:00 KST',
                       venues=venues,macro=self.macro(now),
                       methodology=dict(major_assets=['BTC','ETH'],excluded_assets=sorted(EXCLUDED),
                                        min_coverage=.8,min_alts=20,confirmation_samples=2,
                                        threshold_status='RESEARCH_HYPOTHESIS',
                                        turnover_is_market_cap_dominance=False))
        payload['generated_ts_ms'] = int(time.time()*1000)
        payload['asset_class'] = 'CRYPTO_SPOT'
        payload['source_metadata'] = {
            'upbit': {'quote':'KRW','timezone':'UTC','calendar':'24/7','source':'https://api.upbit.com/v1'},
            'binance': {'quote':'USDT','timezone':'UTC','calendar':'24/7','source':'https://api.binance.com/api/v3'},
            'macro': {'frequency':'DAILY','calendar':'US_BUSINESS_DAYS','source':'FRED','live_signal':False}}
        payload['snapshot_id'] = hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()[:20]
        return confirm(payload,previous,now)


def persist(root, payload):
    root = Path(root)
    target = root/'exports'/'market_context_latest.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    encoded = json.dumps(payload,ensure_ascii=False,allow_nan=False,separators=(',',':'))
    # Bounded summaries, no full-universe tick/candle duplication.
    with sqlite3.connect(root/'market_context.sqlite3',timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS snapshots (bucket INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        bucket = payload['observed_ts_ms']//1000//INTERVAL*INTERVAL
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?)',(bucket,encoded))
        db.execute('DELETE FROM snapshots WHERE bucket < ? OR (bucket < ? AND bucket % 3600 != 0)',
                   (bucket-90*DAY,bucket-7*DAY))
    temp = target.with_suffix('.tmp')
    temp.write_text(encoded,encoding='utf-8');os.replace(temp,target)


async def run(root, log):
    collector = Collector()
    while True:
        started = time.time()
        try:
            try:
                previous = json.loads((Path(root)/'exports'/'market_context_latest.json').read_text())
            except (OSError,ValueError): previous = None
            payload = await asyncio.to_thread(collector.collect,previous=previous)
            await asyncio.to_thread(persist,root,payload)
            log.info('market_context_published id=%s venues=%s macro=%s',payload['snapshot_id'],
                     {v:r['status']+':'+r.get('observed_state',r.get('reason','')) for v,r in payload['venues'].items()},
                     {v:r['status'] for v,r in payload['macro'].items()})
        except asyncio.CancelledError: raise
        except Exception:
            log.exception('market_context_collection_failed')
        await asyncio.sleep(max(1,INTERVAL-(time.time()-started)))
