"""Public market-cap dominance; independent of venue breadth/turnover."""
import json
import math
import re
from datetime import datetime, timezone
import requests

UPBIT_URL = 'https://datalab.upbit.com/insight/bitcoin-dominance'
GLOBAL_URL = 'https://api.coingecko.com/api/v3/global'


def ratio(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('DOMINANCE_RATIO')
    return value


def fresh(timestamp, now):
    timestamp = float(timestamp)
    if not math.isfinite(timestamp) or not -30 <= now-timestamp <= 1800:
        raise ValueError('DOMINANCE_STALE')
    return timestamp


def iso_stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).timestamp()


def point(current, previous):
    current, previous = ratio(current), ratio(previous)
    delta = (current-previous)*100
    return dict(share=current, previous_share=previous, change_pp=delta,
                direction='UP' if delta > 1e-9 else 'DOWN' if delta < -1e-9 else 'FLAT')


def upbit_page(html, now):
    # Read JSON embedded in the public page, never execute its scripts.
    chunks = []
    for raw in re.findall(r'self\.__next_f\.push\((.*?)\)</script>', html, re.S):
        value = json.loads(raw)
        if value[0] == 1 and isinstance(value[1], str):
            chunks.append(value[1])
    def walk(node):
        if isinstance(node, dict):
            if node.get('queryKey') == [['indicator', 'mdom', 'recents']]:
                yield node['state']['data']['data']
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    for line in ''.join(chunks).splitlines():
        _, _, encoded = line.partition(':')
        try:
            node = json.loads(encoded)
        except ValueError:
            continue
        for rows in walk(node):
            assets = {row['code']: row for row in rows}
            stamps = [fresh(iso_stamp(assets[key]['candleDateTime']), now)
                      for key in ('BTC', 'ETH', 'STABLE', 'ETC')]
            if max(stamps)-min(stamps) > 60:
                raise ValueError('DOMINANCE_TIME_MISMATCH')
            values = {key: point(assets[key]['tradePrice'], assets[key]['prevClosingPrice'])
                      for key in ('BTC', 'ETH', 'STABLE', 'ETC')}
            for field in ('share', 'previous_share'):
                if abs(sum(row[field] for row in values.values())-1) > .0001:
                    raise ValueError('DOMINANCE_TOTAL')
            return dict(status='OK', source='Upbit Data Lab', source_url=UPBIT_URL,
                        scope='UPBIT_ALL_MARKETS_MARKET_CAP', comparison='PREVIOUS_CLOSE',
                        observed_ts_ms=int(min(stamps)*1000), assets=values)
    raise ValueError('DOMINANCE_PAGE_SCHEMA')


def global_data(data, coins, now):
    observed = fresh(data['updated_at'], now)
    total_change = float(data['market_cap_change_percentage_24h_usd'])/100
    if not math.isfinite(total_change) or total_change <= -1:
        raise ValueError('DOMINANCE_TOTAL_CHANGE')
    by_id = {row['id']: row for row in coins}
    values = {}
    for key, ident in (('BTC', 'bitcoin'), ('ETH', 'ethereum')):
        coin = by_id[ident]
        coin_time = fresh(iso_stamp(coin['last_updated']), now)
        if abs(coin_time-observed) > 900:
            raise ValueError('DOMINANCE_TIME_MISMATCH')
        change = float(coin['market_cap_change_percentage_24h'])/100
        if not math.isfinite(change) or change <= -1:
            raise ValueError('DOMINANCE_COIN_CHANGE')
        current = ratio(float(data['market_cap_percentage'][key.lower()])/100)
        # Prior share derived from the same provider's 24h capitalization changes.
        # Separate API snapshot times mean this is an estimate, labelled in the UI.
        values[key] = point(current, current*(1+total_change)/(1+change))
    if sum(row['share'] for row in values.values()) > 1:
        raise ValueError('DOMINANCE_TOTAL')
    return dict(status='OK', source='CoinGecko', source_url='https://www.coingecko.com/en/api',
                scope='GLOBAL_MARKET_CAP', comparison='24H_ESTIMATE',
                observed_ts_ms=int(observed*1000), assets=values)


def collect(fetch, now):
    result = {}
    for region in ('upbit', 'global'):
        try:
            if region == 'upbit':
                row = upbit_page(fetch(UPBIT_URL, now=now, text=True, timeout=(3,20)), now)
            else:
                data = fetch(GLOBAL_URL, now=now, timeout=(3,20))['data']
                coins = fetch('https://api.coingecko.com/api/v3/coins/markets',
                              {'vs_currency': 'usd', 'ids': 'bitcoin,ethereum'}, now=now, timeout=(3,20))
                row = global_data(data, coins, now)
            result[region] = row
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            result[region] = dict(status='UNAVAILABLE', reason=type(exc).__name__)
    return result
