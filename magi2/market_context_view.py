"""Authenticated MAGI1 context consumer; contains no portfolio mutations."""
from magi2.latency import timed
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from magi1.market_context import SCHEMA, TTL

KST = ZoneInfo('Asia/Seoul')
DIRECTION = {'UP':'상승','DOWN':'하락','FLAT':'중립','MIXED':'혼조'}
PARTICIPATION = {'ALT_EXPANSION':'알트 확산','MAJOR_LED':'BTC·ETH 집중',
                 'BROAD_WEAKNESS':'동반 약세','MIXED':'혼재'}
LEADERSHIP = {'ALT_EXPANSION':'알트장','MAJOR_LED':'메인장',
              'BROAD_WEAKNESS':'동반 약세','MIXED':'혼재'}


def macro_direction(macro, *keys):
    """Summarize published changes only when every required series is usable."""
    rows = [macro.get(key, {}) for key in keys]
    if any(row.get('status') not in ('DELAYED', 'STALE') for row in rows):
        return '판단 보류 · 자료 부족'
    if any(row['status'] == 'STALE' for row in rows):
        return '판단 보류 · 오래된 자료'
    changes = [row.get('change') for row in rows]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in changes):
        return '판단 보류 · 자료 부족'
    if all(value > 0 for value in changes): return '상승'
    if all(value < 0 for value in changes): return '하락'
    if all(value == 0 for value in changes): return '보합'
    return '혼조'


def validate(payload, now):
    if (payload.get('schema_version') != SCHEMA or payload.get('producer')!='MAGI1'
            or payload.get('mode')!='OBSERVATION_ONLY' or payload.get('execution_eligible') is not False):
        raise ValueError('MARKET_CONTEXT_SCHEMA')
    observed, generated, expires = [payload[k]/1000 for k in
                                    ('observed_ts_ms','generated_ts_ms','expires_ts_ms')]
    if not (math.isfinite(now) and observed <= generated <= now+30
            and observed <= now < expires <= observed+TTL):
        raise ValueError('MARKET_CONTEXT_STALE')
    if (payload['day_start_ts_ms']//1000 != int(observed//86400)*86400
            or int(observed//86400) != int(now//86400)):
        raise ValueError('MARKET_CONTEXT_DAY')
    for venue in ('upbit','binance'):
        row = payload['venues'][venue]
        if row['status'] != 'OK': continue
        if (row['direction'] not in DIRECTION or row['participation'] not in PARTICIPATION
                or row['confirmation'] not in ('PENDING','CONFIRMED') or row['risk'] not in ('HIGH','NORMAL')):
            raise ValueError('MARKET_CONTEXT_STATE')
        b = row['breadth']
        if b['valid'] > b['eligible'] or b['alt_count']<20 or b['coverage']<.8:
            raise ValueError('MARKET_CONTEXT_COVERAGE')
        if not math.isfinite(b['median_return']): raise ValueError('MARKET_CONTEXT_NUMBER')
        for key in ('coverage','advancing_ratio','outperform_btc_ratio','major_turnover_share'):
            if not 0 <= b[key] <= 1: raise ValueError('MARKET_CONTEXT_RATIO')
        for a in ('btc','eth'):
            for key,value in row[a].items():
                if not math.isfinite(value): raise ValueError('MARKET_CONTEXT_NUMBER')
    return payload


@timed('market_context.fetch')
def fetch(now=None):
    now = time.time() if now is None else now
    path = os.getenv('MAGI1_MARKET_CONTEXT_PATH','').strip()
    if path:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    else:
        base = os.getenv('MAGI1_INTELLIGENCE_URL','').strip()
        token = os.getenv('MAGI_INTELLIGENCE_TOKEN','')
        url = urlsplit(base)
        if url.scheme not in ('http','https') or not url.netloc or len(token)<32:
            raise ValueError('MAGI1_CONNECTION_MISSING')
        address = urlunsplit((url.scheme,url.netloc,'/market-context','',''))
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(address,headers={'Authorization':'Bearer '+token},
                                   timeout=(3,5),allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
    return validate(payload,now)


def render(payload, now=None):
    now = time.time() if now is None else now
    validate(payload,now)
    stamp = datetime.fromtimestamp(payload['observed_ts_ms']/1000,KST)
    lines = ['🧭 시장 국면 · MAGI1', f'관측 {stamp:%m/%d %H:%M} KST · 5분 갱신',
             '\n🧭 종합의견']
    for venue,label in (('upbit','국내 BTC'),('binance','해외 BTC')):
        row = payload['venues'][venue]
        if row['status'] != 'OK':
            lines.append(f'{label}: 판단 보류 · 자료 부족')
            continue
        lines.append(f'{label}: 오늘 {DIRECTION[row["today_direction"]]} · 중기 {DIRECTION[row["medium_direction"]]} · 단기 {DIRECTION[row["short_direction"]]}')
    for venue,label in (('upbit','국내'),('binance','해외')):
        row = payload['venues'][venue]
        if row['status'] != 'OK':
            lines.append(f'{label} 주도장: 판단 보류')
            continue
        flag = '확인' if row['confirmation'] == 'CONFIRMED' else '전환 관측'
        lines.append(f'{label} 주도장: {LEADERSHIP[row["participation"]]} · {flag}')
        if row['risk'] == 'HIGH': lines.append(f'⚠ {label} 위험 높음')
    macro = payload['macro']
    lines.extend([
        f'금리(미국 10년): {macro_direction(macro, "DGS10")}',
        f'미국 주식(S&P500·NASDAQ): {macro_direction(macro, "SP500", "NASDAQCOM")}',
        f'달러: {macro_direction(macro, "DTWEXBGS")} · 유가: {macro_direction(macro, "DCOILWTICO")}',
        '거시 방향은 최근 공표치의 전 관측치 대비',
        '\n📊 근거 데이터',
        '오늘: 09:00 KST 이후 · 중기: 완료 7일 · 단기: 완료 4시간'])
    for venue,label in (('upbit','국내 · 업비트 KRW'),('binance','해외 · Binance USDT 현물')):
        row = payload['venues'][venue]
        lines.append('\n'+label)
        if row['status']!='OK':
            lines.append('판단 보류 · 시세/이력/종목 커버리지 부족');continue
        flag = '2회 연속 확인' if row['confirmation']=='CONFIRMED' else '전환 관측 · 다음 표본 확인 대기'
        lines.append(f'{DIRECTION[row["direction"]]} / {PARTICIPATION[row["participation"]]} · {flag}')
        btc,eth,b = row['btc'],row['eth'],row['breadth']
        lines.extend([
            f'BTC 중기 {DIRECTION[row["medium_direction"]]} · 단기 {DIRECTION[row["short_direction"]]} · 오늘 {DIRECTION[row["today_direction"]]}',
            f'BTC 오늘 {btc["day_return"]:+.2%} · 1h {btc["return_1h"]:+.2%} · 4h {btc["return_4h"]:+.2%}',
            f'BTC 7일 {btc["return_7d"]:+.2%} · 30일 {btc["return_30d"]:+.2%} (완료 일봉)',
            f'ETH 오늘 {eth["day_return"]:+.2%} · BTC 당일 고점 대비 {btc["day_drawdown"]:+.2%}',
            f'알트 상승 {b["advancing_ratio"]:.0%} · BTC 초과 {b["outperform_btc_ratio"]:.0%} · 중앙값 {b["median_return"]:+.2%}',
            f'BTC·ETH 거래대금 비중 {b["major_turnover_share"]:.1%} · 유효 {b["valid"]}/{b["eligible"]}종목',
            f'BTC 24h 실현변동 {btc["realized_vol_24h"]:.2%} · 위험 {"높음" if row["risk"]=="HIGH" else "보통"}'])
    lines.append('\n거시 참고 · FRED 공표 일별 자료, 실시간 아님')
    for key,row in payload['macro'].items():
        if row['status']=='UNAVAILABLE': lines.append(f'{row["label"]}: 자료 없음');continue
        change = f'{row["change"]:+.2f}%p' if row['change_unit']=='PERCENTAGE_POINTS' else f'{row["change"]:+.2%}'
        stale = ' · 오래된 자료' if row['status']=='STALE' else ''
        lines.append(f'{row["label"]}: {row["value"]:,.2f} ({change}) · {row["observation_date"]}{stale}')
    lines.extend(['\n메이저=BTC·ETH · 거래대금 비중은 시총 도미넌스와 다름',
                  '국면 기준은 검증 중 · 전략 비중 자동 변경 없음'])
    return '\n'.join(lines)


def view():
    try:
        return render(fetch())
    except (requests.RequestException,OSError,ValueError,KeyError,TypeError):
        return '🧭 시장 국면 · 판단 보류\nMAGI1 자료가 준비되지 않았거나 10분 유효기간이 지났습니다.'
