"""On-demand, price-only market context. No trading or allocation dependencies."""
from dataclasses import dataclass
from datetime import datetime
import math
import time
from zoneinfo import ZoneInfo

import numpy as np
import requests

DAY = 86400
HISTORY_DAYS = 400
SETTLEMENT_SECONDS = 180
PAIRS = ('XXBTZUSD', 'XETHZUSD')
URL = 'https://api.kraken.com/0/public/OHLC'
LABELS = {'UP_LOW': '상승·저변동', 'UP_HIGH': '상승·고변동',
          'DOWN_LOW': '하락·저변동', 'DOWN_HIGH': '하락·고변동'}
KST = ZoneInfo('Asia/Seoul')


class DataUnavailable(ValueError):
    """Safe, user-facing reason to withhold a classification."""


@dataclass(frozen=True)
class Regime:
    state: str
    observed_state: str
    btc_return: float
    eth_return: float
    volatility: float
    threshold: float
    close_time: int
    requested_at: float


def cutoff(now):
    # Daily candles settle at 00:00 UTC; allow three minutes for publication.
    return int((now - SETTLEMENT_SECONDS) // DAY) * DAY


def completed_prices(payload, pair, now):
    """Require both assets' exact same 400 completed dates, without filling gaps."""
    end = cutoff(now)
    start = end - HISTORY_DAYS * DAY
    try:
        if payload.get('error'):
            raise DataUnavailable('거래소에서 일봉 자료를 제공하지 못했습니다.')
        rows = payload['result'][pair]
        if not isinstance(rows, list):
            raise ValueError('rows')
        prices = {}
        for row in rows:
            stamp = float(row[0])
            if not math.isfinite(stamp) or stamp != int(stamp) or stamp % DAY:
                raise ValueError('timestamp')
            stamp = int(stamp)
            if stamp > int(now // DAY) * DAY:
                raise ValueError('future timestamp')
            # Kraken always includes the currently forming candle. Never use it.
            if stamp < start or stamp + DAY > end:
                continue
            if stamp in prices:
                raise ValueError('duplicate')
            price = float(row[4])
            if not math.isfinite(price) or price <= 0:
                raise ValueError('price')
            prices[stamp] = price
        expected = list(range(start, end, DAY))
        if not prices or max(prices) != end - DAY:
            raise DataUnavailable('최신 완료 일봉이 없어 판단을 보류합니다.')
        if sorted(prices) != expected:
            raise DataUnavailable('일봉 이력이 부족하거나 중간 날짜가 누락됐습니다.')
        return [prices[t] for t in expected]
    except DataUnavailable:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OverflowError) as exc:
        raise DataUnavailable('일봉 자료의 시각·가격 형식을 확인할 수 없습니다.') from exc


def classify(btc, eth, now):
    prices = np.asarray([btc, eth], dtype=float).T
    if (prices.shape != (HISTORY_DAYS, 2) or not np.isfinite(prices).all()
            or (prices <= 0).any()):
        raise DataUnavailable('BTC·ETH 일봉 이력이 부족하거나 유효하지 않습니다.')
    returns = np.diff(np.log(prices), axis=0).mean(axis=1)
    vols = np.full(HISTORY_DAYS, np.nan)
    for j in range(30, HISTORY_DAYS):
        vols[j] = np.std(returns[j-30:j], ddof=1) * math.sqrt(365)
    state = pending = None
    streak = 0
    for j in range(282, HISTORY_DAYS):
        # Threshold uses 252 previous volatility observations, excluding today.
        threshold = float(np.quantile(vols[j-252:j], .65))
        trend = np.log(prices[j]) - np.log(prices[j-90])
        raw = ('UP' if trend.mean() > 0 else 'DOWN') + (
            '_HIGH' if vols[j] > threshold else '_LOW')
        streak = streak + 1 if raw == pending else 1
        pending = raw
        if streak >= 2:
            state = raw
    if state is None:
        raise DataUnavailable('국면이 연속 2일 확인되지 않아 판단을 보류합니다.')
    growth = np.expm1(trend)
    return Regime(state, raw, float(growth[0]), float(growth[1]),
                  float(vols[-1]), threshold, cutoff(now), now)


def fetch_regime(now=None, get=None):
    now = time.time() if now is None else now
    get = requests.get if get is None else get
    series = []
    for pair in PAIRS:
        response = get(URL, params={'pair': pair, 'interval': 1440,
                       'since': cutoff(now) - HISTORY_DAYS * DAY}, timeout=(3, 5))
        response.raise_for_status()
        series.append(completed_prices(response.json(), pair, now))
    return classify(*series, now)


def keyboard():
    return {'inline_keyboard': [[
        {'text': '🔄 다시 조회', 'callback_data': 'nav:regime'},
        {'text': '↩️ 메인 메뉴', 'callback_data': 'nav:menu'}]]}


def render(result):
    lines = ['🧭 시장 국면 · 투자 참고', LABELS[result.state],
             f'추세: 90일 BTC {result.btc_return:+.1%} · ETH {result.eth_return:+.1%}',
             f'변동: 30일 연환산 {result.volatility:.1%} · 고변동 기준 {result.threshold:.1%}']
    if result.state != result.observed_state:
        lines.append(f'{LABELS[result.observed_state]} 전환 관측 · 2일 확인 대기')
    if result.btc_return * result.eth_return < 0:
        lines.append('BTC·ETH 추세가 엇갈림 · 두 자산의 평균 추세로 판단')
    lines.extend(['Kraken BTC·ETH/USD 가격·변동성 기준',
                  f'일봉 마감 {datetime.fromtimestamp(result.close_time, KST):%m/%d %H:%M} KST'
                  f' · 조회 {datetime.fromtimestamp(result.requested_at, KST):%H:%M}'])
    return '\n'.join(lines)


def unavailable(reason='현재 시세 자료를 조회하지 못했습니다.'):
    return '🧭 시장 국면 · 판단 보류\n' + reason + '\n잠시 후 다시 조회해 주세요.'


def legacy_view():
    try:
        return render(fetch_regime())
    except DataUnavailable as exc:
        return unavailable(str(exc))
    except (requests.RequestException, ValueError, TypeError):
        return unavailable()


def view():
    from magi2.market_context_view import view as context_view
    return context_view()
