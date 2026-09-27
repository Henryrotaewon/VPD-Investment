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
from magi1.dominance import fresh, point

KST = ZoneInfo('Asia/Seoul')
DIRECTION = {'UP':'상승','DOWN':'하락','FLAT':'중립','MIXED':'혼조'}
PARTICIPATION = {'ALT_EXPANSION':'알트 확산','MAJOR_LED':'BTC·ETH 집중',
                 'BROAD_WEAKNESS':'동반 약세','MIXED':'혼재','UNAVAILABLE':'자료 부족'}
LEADERSHIP = {'ALT_EXPANSION':'알트장','MAJOR_LED':'메인장',
              'BROAD_WEAKNESS':'동반 약세','MIXED':'혼재','UNAVAILABLE':'자료 부족'}


def dominance_row(payload, region, now):
    row = payload.get('dominance', {}).get(region, {})
    try:
        if row.get('status') != 'OK': return None
        expected = ('UPBIT_ALL_MARKETS_MARKET_CAP', 'PREVIOUS_CLOSE') if region == 'upbit' else ('GLOBAL_MARKET_CAP', '24H_ESTIMATE')
        if (row['scope'], row['comparison']) != expected: return None
        fresh(row['observed_ts_ms']/1000, now)
        for key in ('BTC', 'ETH'):
            asset = row['assets'][key]
            check = point(asset['share'], asset['previous_share'])
            if (not math.isfinite(asset['change_pp']) or
                    abs(check['change_pp']-asset['change_pp']) > 1e-7 or
                    check['direction'] != asset['direction']): return None
        return row
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def macro_direction(macro, *keys):
    """Summarize published changes only when every required series is usable."""
    rows = [macro.get(key, {}) for key in keys]
    if any(row.get('status') not in ('DELAYED', 'STALE') for row in rows):
        return '판단 보류 · 자료 부족'
    changes = [row.get('change') for row in rows]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in changes):
        return '판단 보류 · 자료 부족'
    result = ('상승' if all(value > 0 for value in changes) else
              '하락' if all(value < 0 for value in changes) else
              '보합' if all(value == 0 for value in changes) else '혼조')
    return result + ('(과거 공표)' if any(row['status'] == 'STALE' for row in rows) else '')


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
        if row['status'] not in ('OK', 'PARTIAL'): continue
        if (row['direction'] not in DIRECTION or row['participation'] not in PARTICIPATION
                or row['confirmation'] not in ('PENDING','CONFIRMED') or row['risk'] not in ('HIGH','NORMAL')):
            raise ValueError('MARKET_CONTEXT_STATE')
        for a in ('btc','eth'):
            for key,value in row[a].items():
                if not math.isfinite(value): raise ValueError('MARKET_CONTEXT_NUMBER')
        b = row['breadth']
        if row['status'] == 'PARTIAL':
            if b is not None or row['participation'] != 'UNAVAILABLE':
                raise ValueError('MARKET_CONTEXT_PARTIAL')
            continue
        if b['valid'] > b['eligible'] or b['alt_count']<20 or b['coverage']<.8:
            raise ValueError('MARKET_CONTEXT_COVERAGE')
        if not math.isfinite(b['median_return']): raise ValueError('MARKET_CONTEXT_NUMBER')
        for key in ('coverage','advancing_ratio','outperform_btc_ratio','major_turnover_share'):
            if not 0 <= b[key] <= 1: raise ValueError('MARKET_CONTEXT_RATIO')
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
        if row['status'] not in ('OK', 'PARTIAL'):
            lines.append(f'{label}: 자료 없음')
            continue
        lines.append(f'{label}: 오늘 {DIRECTION[row["today_direction"]]} · 중기 {DIRECTION[row["medium_direction"]]} · 단기 {DIRECTION[row["short_direction"]]}')
    dominance_rows = {region: dominance_row(payload, region, now) for region in ('upbit', 'global')}
    leadership = []
    for region,label in (('upbit','국내'),('global','해외(글로벌)')):
        row = dominance_rows[region]
        state = DIRECTION[row['assets']['BTC']['direction']] if row else '자료 없음'
        if state == '중립': state = '보합'
        leadership.append(f'{label} {state}')
    macro = payload['macro']
    lines.extend([
        'BTC 도미넌스: ' + ' · '.join(leadership),
        f'미국 금리 {macro_direction(macro, "DGS10")} · 미국 주식 {macro_direction(macro, "SP500", "NASDAQCOM")}',
        f'달러 {macro_direction(macro, "DTWEXBGS")} · 유가 {macro_direction(macro, "DCOILWTICO")}',
        '\n📊 근거 데이터',
        '오늘: 09:00 KST 이후 · 중기: 완료 7일 · 단기: 완료 4시간',
        'BTC 방향은 해당 기간 등락률의 부호 · 거시는 최근 공표치의 전 관측치 대비'])
    for venue,label in (('upbit','국내 · 업비트 KRW'),('binance','해외 · Binance USDT 현물')):
        row = payload['venues'][venue]
        lines.append('\n'+label)
        if row['status'] not in ('OK', 'PARTIAL'):
            lines.append('판단 보류 · 시세/이력/종목 커버리지 부족');continue
        flag = '2회 연속 확인' if row['confirmation']=='CONFIRMED' else '전환 관측 · 다음 표본 확인 대기'
        lines.append(f'{DIRECTION[row["direction"]]} / {PARTICIPATION[row["participation"]]} · {flag}')
        btc,eth,b = row['btc'],row['eth'],row['breadth']
        lines.extend([
            f'BTC 중기 {DIRECTION[row["medium_direction"]]} · 단기 {DIRECTION[row["short_direction"]]} · 오늘 {DIRECTION[row["today_direction"]]}',
            f'BTC 오늘 {btc["day_return"]:+.2%} · 1h {btc["return_1h"]:+.2%} · 4h {btc["return_4h"]:+.2%}',
            f'BTC 7일 {btc["return_7d"]:+.2%} · 30일 {btc["return_30d"]:+.2%} (완료 일봉)',
            f'ETH 오늘 {eth["day_return"]:+.2%} · BTC 당일 고점 대비 {btc["day_drawdown"]:+.2%}'])
        if b is not None:
            lines.extend([f'알트 상승 {b["advancing_ratio"]:.0%} · BTC 초과 {b["outperform_btc_ratio"]:.0%} · 중앙값 {b["median_return"]:+.2%}',
            f'BTC·ETH 거래대금 비중 {b["major_turnover_share"]:.1%} · 유효 {b["valid"]}/{b["eligible"]}종목'])
        else:
            coverage = row.get('coverage', {})
            lines.append(f'메인·알트 자료 부족 · 유효 {coverage.get("valid", 0)}/{coverage.get("eligible", 0)}종목 · BTC 판단은 유지')
        lines.append(f'BTC 24h 실현변동 {btc["realized_vol_24h"]:.2%} · 위험 {"높음" if row["risk"]=="HIGH" else "보통"}')
    lines.append('\n도미넌스 · 시가총액 비중')
    for region, label in (('upbit', '국내 · 업비트 전체 마켓'), ('global', '해외 · 글로벌')):
        row = dominance_rows[region]
        if row is None:
            lines.append(f'{label}: 자료 없음'); continue
        basis = '전일 종가 대비' if region == 'upbit' else '24시간 대비 환산 추정'
        observed = datetime.fromtimestamp(row['observed_ts_ms']/1000, KST)
        lines.append(f'{label} · {basis} · {observed:%m/%d %H:%M} KST')
        parts = []
        for key, name in (('BTC', 'BTC'), ('ETH', 'ETH'), ('STABLE', '스테이블'), ('ETC', '기타')):
            if key not in row['assets']: continue
            asset = row['assets'][key]
            parts.append(f'{name} {asset["share"]:.2%} ({asset["change_pp"]:+.3f}%p)')
        lines.append(' · '.join(parts))
    lines.append('출처: 업비트 데이터랩 · Data provided by CoinGecko https://www.coingecko.com/en/api')
    lines.append('\n거시 참고 · FRED 공표 일별 자료, 실시간 아님')
    for key,row in payload['macro'].items():
        if row['status']=='UNAVAILABLE': lines.append(f'{row["label"]}: 자료 없음');continue
        change = f'{row["change"]:+.2f}%p' if row['change_unit']=='PERCENTAGE_POINTS' else f'{row["change"]:+.2%}'
        stale = ' · 오래된 자료' if row['status']=='STALE' else ''
        lines.append(f'{row["label"]}: {row["value"]:,.2f} ({change}) · {row["observation_date"]}{stale}')
    lines.extend(['\nBTC 도미넌스 상승=BTC 비중 확대 · 하락만으로 알트 상승장을 뜻하지 않음',
                  '국내는 업비트 상장자산 기준 · 해외는 글로벌 기준 · 거래대금 비중과 다름',
                  '국면 기준은 검증 중 · 전략 비중 자동 변경 없음'])
    return '\n'.join(lines)


def view():
    try:
        return render(fetch())
    except (requests.RequestException,OSError,ValueError,KeyError,TypeError):
        return '🧭 시장 국면 · 판단 보류\nMAGI1 자료가 준비되지 않았거나 10분 유효기간이 지났습니다.'
