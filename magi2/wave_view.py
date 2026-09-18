"""Cached WAVE research views; no analysis or network request on button presses."""
from datetime import datetime
import json
from math import isfinite
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import requests

SCHEMA = 'wave-timeshift-catalog-v1'
VENUES = {'upbit': '업비트', 'bithumb': '빗썸', 'coinone': '코인원',
          'binance': '바이낸스', 'bybit': '바이빗', 'kraken': '크라켄'}
HORIZONS = {'MICRO': '10초', 'MESO': '1분', 'MACRO': '10분'}
PAGE_SIZE = 4


def clock(ts):
    return datetime.fromtimestamp(ts / 1000, ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')


def number(value, suffix='', scale=1, signed=False):
    if not isinstance(value, (int, float)) or not isfinite(value):
        return '미확인'
    return (f'{value * scale:+.1f}' if signed else f'{value * scale:.1f}') + suffix


def keyboard(section='overview', offset=0, total=0):
    rows = [[{'text': '📡 신호 강도', 'callback_data': 'wave:strength:0'},
             {'text': '🌊 전파 근거', 'callback_data': 'wave:evidence:0'}],
            [{'text': '💰 매매 가능성', 'callback_data': 'wave:trading:0'},
             {'text': '📖 전략 설명', 'callback_data': 'wave:guide:0'}],
            [{'text': '🌐 크로스마켓 연구', 'callback_data': 'guide:cross'}]]
    nav = []
    if offset > 0:
        nav.append({'text': '◀ 이전', 'callback_data': f'wave:{section}:{max(0, offset-PAGE_SIZE)}'})
    if offset + PAGE_SIZE < total:
        nav.append({'text': '다음 ▶', 'callback_data': f'wave:{section}:{offset+PAGE_SIZE}'})
    if nav:
        rows.append(nav)
    rows.append([{'text': '↻ WAVE 요약', 'callback_data': 'nav:wave'},
                 {'text': '↩️ 전략검증', 'callback_data': 'nav:strategies'}])
    return {'inline_keyboard': rows}


def render(report, section='overview', offset=0, now=None):
    if section == 'guide':
        from magi2.strategy_guide import strategy_text
        return strategy_text('wave'), keyboard()
    if not report:
        return ('🌐 WAVE 분석\n분석 자료를 준비하고 있습니다.\n'
                '신호 강도·전파 근거·매매 가능성을 나눠 확인합니다.'), keyboard()
    now = now if now is not None else time.time_ns() // 1000000
    stale = now >= report['expires_ts_ms']
    header = f"🌐 WAVE · 최근 24시간\n{clock(report['window_start_ms'])} ~ {clock(report['window_end_ms'])} KST"
    if stale:
        header += '\n⚠ 마지막 분석본 · 최신 연결 확인 중'
    lines = [header]
    total = 0
    if section == 'strength':
        rows = report['recent']
        total = len(rows)
        offset = min(max(0, offset), max(0, (total-1)//PAGE_SIZE*PAGE_SIZE))
        lines += ['', '📡 신호 강도 · 최신 감지 순', '크기·매수세 관측값이며 예측확률이 아닙니다.']
        if not rows:
            lines += ['이 구간의 감지 기록이 없습니다.']
        for row in rows[offset:offset+PAGE_SIZE]:
            side = '매수세' if row['direction'] == 'BUY' else '매도세'
            venue = VENUES.get(row['origin_venue'], row['origin_venue'])
            volume = row['volume_acceleration']
            volume_text = number(volume, '배') if isinstance(volume, (int, float)) and volume > 0 else '기준 부족'
            lines += ['', f"{clock(row['event_ts_ms'])} {row['asset']} · {side} · {HORIZONS.get(row['horizon'], row['horizon'])}",
                      f'최초 감지: {venue}',
                      f"가격 {number(row['return_bps'], '%', .01, True)} · 거래량 {volume_text}",
                      f"순매수 체결 비중 {number(row['net_flow_ratio'], '%', 100, True)} · 스프레드 {number(row['spread_bps'], '%', .01)}"]
            hits = row['followers']
            if hits:
                shown = [f"{VENUES.get(x['venue'], x['venue'])} +{x['lag_ms']/1000:.1f}초" +
                         ('' if x['timing_resolved'] else '¹') for x in hits]
                lines += ['30초 내 후속 감지: ' + ' / '.join(shown)]
            else:
                lines += ['30초 내 후속 감지: ' + ('기록 없음' if row['finalized'] else '집계 중')]
        lines += ['', '순매수 비중=(매수량−매도량)/총체결량. 거래량 배수는 직전 동기간 대비입니다.',
                  '¹ 선행 시간 판별 보류: 시간 해상도·수신 지연을 충분히 구분하지 못함.',
                  '평소 대비 이례성·유지 예측확률은 아직 미검증입니다.']
    elif section == 'evidence':
        rows = report['edges']
        total = len(rows)
        offset = min(max(0, offset), max(0, (total-1)//PAGE_SIZE*PAGE_SIZE))
        lines += ['', '🌊 전파 근거 · 비교 표본 많은 순',
                  '선별된 사건 목록의 탐색용 비교입니다. 같은 종목·방향·관측 길이끼리 비교합니다.']
        if not rows:
            lines += ['비교할 전파 기록이 없습니다.']
        for row in rows[offset:offset+PAGE_SIZE]:
            route = VENUES.get(row['origin_venue'], row['origin_venue']) + ' → ' + VENUES.get(row['follower_venue'], row['follower_venue'])
            side = '매수세' if row['direction'] == 'BUY' else '매도세'
            lines += ['', f"{row['asset']} · {side} · {HORIZONS.get(row['horizon'], row['horizon'])} | {route}",
                      f"원관측 {row['observed_n']}건 · 관측 불가 {row['unobservable']}건",
                      f"비교 가능 {row['matched_n']}건 · 대조 구간 {row['control_windows']}개"]
            if row['matched_n']:
                lines += [f"30초 반응: 실제 {number(row['actual_rate'], '%', 100)} / 대조 {number(row['control_rate'], '%', 100)}",
                          f"차이 {number(row['lift_pp'], '%p', signed=True)}" + (' · 표본 부족' if row['matched_n'] < 30 else ' · 탐색 결과'),
                          f"시간차 중앙값 {number(row['lag_median_ms'], '초', .001)} · 지연 구분 가능 {row['resolved_lag_n']}건"]
            else:
                lines += ['대조 구간 관측·표본 부족으로 차이를 계산하지 않습니다.']
        lines += ['', '시간 이동: 과거 11·17·29·43·59분. 동일 4시간대, 관측이 확인된 30초 구간만 사용합니다.',
                  '실제·대조는 같은 비교 표본 기준. 대조 비율은 사건별 평균입니다.',
                  '사건 선별 편향·장세 차이·반복 관측이 남아 있어 인과관계나 매매 성공확률을 뜻하지 않습니다.']
    elif section == 'trading':
        lines += ['', '💰 매매 가능성: 검증 전',
                  '현재 WAVE에는 확산 확인 후 진입·체결·청산을 평가하는 매매 검증이 연결되지 않았습니다.',
                  '', '참고 · 최초 감지 후 업비트 가격 반응',
                  'BUY 관측만 · 최초 매도호가 진입/이후 매수호가 청산 가정 · 실제 체결 아님']
        for row in report['price_response']:
            label = f"{row['horizon_sec']}초" if row['horizon_sec'] < 60 else f"{row['horizon_sec']//60}분"
            lines += [f"• {label}: {row['n']}건 · 평균 {number(row['mean_return_pct'], '%', signed=True)} · 양수 비율 {number(row['positive_rate'], '%', 100)}"]
        lines += ['', '기간 내 평가 완료분. 수수료·주문 지연·호가 깊이 슬리피지 제외. 같은 움직임의 반복 관측을 포함합니다.',
                  '남은 검증: 확산 확인 시점 진입 → 실제 주문 가능 가격 → 비용 차감 손익 → 다른 기간 재검증.',
                  '분석망은 모든 거래소 간 비교입니다. 위 가격 평가는 기존 업비트 연구 표본입니다.']
    else:
        count = report['counts']
        lines += ['', f"움직임 후보 {count['episodes']:,}건 · 전파 관측 {count['trials']:,}건",
                  f"시간 이동 비교 {count['matched_trials']:,}건 · 거래소쌍·종목·방향·관측 길이별 집계",
                  '', '📡 신호 강도: 가격 변화·체결 압력·거래량',
                  '🌊 전파 근거: 실제 반응과 시간 이동 대조군의 차이',
                  '💰 매매 가능성: 아직 검증 전 · 기존 가격 반응 참고',
                  '', '한 움직임이 여러 관측에 포함됩니다. 비교 건수는 독립 매매 기회 수가 아닙니다.',
                  '대조군은 선별 사건 목록 기준이며 장세 보정·예측확률 검증은 남아 있습니다.',
                  'WHALE은 보조자료로 보존하며 추가 효과는 별도 검증 대상입니다.']
    return '\n'.join(lines), keyboard(section, offset, total)


def validate(payload, now):
    if (payload.get('schema_version') != SCHEMA or payload.get('mode') != 'RESEARCH_ONLY'
            or payload.get('execution_eligible') is not False or payload.get('calibrated_probability') is not None):
        raise ValueError('INVALID_WAVE_CONTRACT')
    generated, expires = payload['generated_ts_ms'], payload['expires_ts_ms']
    if not generated <= now < expires or expires - generated != 900000:
        raise ValueError('STALE_OR_FUTURE_WAVE')
    if payload['window_end_ms'] != generated or generated - payload['window_start_ms'] != 86400000:
        raise ValueError('INVALID_WAVE_WINDOW')
    for key in ('edges', 'recent', 'price_response'):
        if not isinstance(payload[key], list):
            raise ValueError('INVALID_WAVE_ROWS')
    if not isinstance(payload['counts'], dict):
        raise ValueError('INVALID_WAVE_COUNTS')
    return payload


class WaveClient:
    def __init__(self, intelligence_url, token, logger=lambda _: None):
        url = urlsplit(intelligence_url)
        self.url = urlunsplit((url.scheme, url.netloc, url.path.rsplit('/', 1)[0] + '/wave', '', ''))
        self.token = token
        self.log = logger
        self.report = None
        self.stop_event = threading.Event()
        self.thread = None

    def refresh(self):
        if len(self.token) < 32 or not self.url.startswith(('http://', 'https://')):
            raise ValueError('WAVE_TRANSPORT_NOT_CONFIGURED')
        with requests.Session() as session:
            session.trust_env = False
            with session.get(self.url, headers={'Authorization': 'Bearer ' + self.token},
                             timeout=(2, 3), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError('WAVE_UNAVAILABLE')
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > 2000000:
                        raise ValueError('WAVE_TOO_LARGE')
        report = validate(json.loads(content), time.time_ns() // 1000000)
        first = self.report is None
        self.report = report  # One atomic reference replacement; never mutate a published report.
        if first:
            self.log(f"wave_view_ready episodes={report['counts']['episodes']} matched={report['counts']['matched_trials']}")

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.refresh()
            except Exception as exc:
                self.log(f'wave_view_refresh_failed type={type(exc).__name__}')
            self.stop_event.wait(30)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='wave-view', daemon=True)
        self.thread.start()
        return self

    def view(self, section='overview', offset=0):
        return render(self.report, section, offset)
