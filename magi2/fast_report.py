"""Read-only Telegram views of completed native FAST replay runs.

Never imports execution engines, starts capture, or turns alert marks into fills.
"""
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

REASONS = {'TAKE_PROFIT': '목표 익절', 'STOP_LOSS': '손절', 'TIME_LIMIT': '시간 종료',
           'END_OF_TAPE': '관측 종료', 'FEED_GAP': '수신 공백',
           'EXIT_UNOBSERVABLE': '청산 호가 부족'}


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('INVALID_NUMBER')
    return value


def read_json(path, limit):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError('UNSAFE_OR_OVERSIZED_REPORT')
    with path.open('rb') as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('OVERSIZED_REPORT')
    return json.loads(raw)


def clock(ms):
    return datetime.fromtimestamp(number(ms) / 1000, ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M:%S')


def load_latest(root):
    """Only finalized live_job runs with their matching Upbit replay file."""
    root = Path(root)
    if not root.exists():
        return None
    paths = list(root.glob('*/result.json'))
    if len(paths) > 200:
        raise ValueError('TOO_MANY_RUNS')
    runs = []
    for path in paths:
        if path.parent.is_symlink():
            raise ValueError('SYMLINK_RUN')
        result = read_json(path, 1_000_000)
        if not isinstance(result, dict) or not isinstance(result.get('results'), list):
            raise ValueError('INVALID_MANIFEST')
        if any(r.get('venue') == 'upbit' for r in result['results']):
            finished = number(result['finished_ms'])
            if finished <= 0:
                raise ValueError('INVALID_TIME')
            runs.append((finished, path, result))
    if not runs:
        return None
    finished, path, result = max(runs, key=lambda x: x[0])
    report = read_json(path.parent / 'upbit-report.json', 10_000_000)
    if report.get('schema') != 'fast-lab-report-v1' or report.get('mode') != 'OFFLINE_REPLAY':
        raise ValueError('NOT_A_REPLAY_REPORT')
    if not isinstance(report.get('trials'), list) or not isinstance(report.get('policy'), dict):
        raise ValueError('INVALID_REPORT')
    valid = []; excluded = Counter(); seen = set()
    for row in report['trials']:
        features = row['features']
        if features.get('venue') != 'upbit' or features.get('quote') != 'KRW':
            raise ValueError('MIXED_MARKET_REPORT')
        ident = row['id']
        if ident in seen:
            raise ValueError('DUPLICATE_TRIAL')
        seen.add(ident)
        if row.get('mode') != 'OFFLINE_REPLAY' or row.get('policy_id') != report.get('policy_id'):
            raise ValueError('MIXED_POLICY')
        outcome = row.get('policy_exit') or {}
        if outcome.get('status') != 'COMPLETE':
            excluded[outcome.get('status') or '미완료'] += 1
            continue
        entry = row['entry']; cost = number(entry['cost_quote'])
        ret = number(outcome['net_return_bps'])
        entry_ms = number(entry['received_ts_ms']); exit_ms = number(outcome['received_ts_ms'])
        if cost <= 0 or ret < -10000 or not 0 < entry_ms <= exit_ms <= finished:
            raise ValueError('INVALID_COMPLETED_TRADE')
        valid.append({'symbol': features['symbol'], 'entry_ms': entry_ms, 'exit_ms': exit_ms,
                      'cost': cost, 'net_bps': ret, 'pnl': cost * ret / 10000,
                      'reason': outcome.get('reason', '미확인')})
    return {'run_id': str(result.get('run_id', path.parent.name)), 'finished_ms': finished,
            'policy': report['policy'], 'trials': len(report['trials']),
            'valid': sorted(valid, key=lambda x: x['exit_ms']), 'excluded': excluded}


def keyboard():
    return {'inline_keyboard': [
        [{'text': '📊 FAST 모의검증 결과', 'callback_data': 'nav:fast_report'},
         {'text': '📒 FAST 거래내역', 'callback_data': 'nav:fast_orders'}],
        [{'text': '⚡ 포착 목록', 'callback_data': 'nav:fast'},
         {'text': '🔎 5분 신호 평가', 'callback_data': 'nav:fast_compare'}],
        [{'text': '↩️ 메인 메뉴', 'callback_data': 'nav:menu'}]]}


def view(root, detail=False):
    title = '📒 FAST 과거 재생 거래내역' if detail else '🧪 FAST 과거 재생검증'
    try:
        data = load_latest(root)
        if data is None:
            return (title + '\n업비트 KRW · 검증 결과 대기\n'
                    '완료된 모의매매 재생 결과가 아직 없습니다.\n'
                    '현재 /fast는 포착 조회, /fast_compare는 수수료 미차감 5분 평가입니다.\n'
                    '실시간 FAST 모의투자는 /fast_report에서 확인하세요.\n'
                    '조회만 수행하며 매매·수집을 시작하지 않습니다.'), keyboard()
        p = data['policy']
        lines = [title, '업비트 KRW · 오프라인 재생 모의매매',
                 '완료 시각 ' + clock(data['finished_ms']) + ' KST',
                 '실험 ' + data['run_id'][:80],
                 f"후보 {data['trials']}건 / 청산 평가 가능 {len(data['valid'])}건 / 제외 {sum(data['excluded'].values())}건"]
        if detail:
            trades = data['valid'][-10:]
            lines.append(f'최근 청산 {len(trades)}건 (선택된 실험 내)')
            for t in reversed(trades):
                reason = REASONS.get(t['reason'], t['reason'])
                lines += [f"{t['symbol']} · {reason}",
                          f"매수 {clock(t['entry_ms'])} → 청산 {clock(t['exit_ms'])} KST",
                          f"원가 {t['cost']:,.0f}원 · 순손익 {t['pnl']:+,.0f}원 ({t['net_bps']/100:+.2f}%)"]
        elif data['valid']:
            trades = data['valid']; pnl = sum(t['pnl'] for t in trades)
            wins = sum(t['pnl'] > 0 for t in trades)
            lines += [f'실험 손익 합계 {pnl:+,.0f}원',
                      f'양수 거래 {wins}/{len(trades)}건 ({wins/len(trades)*100:.1f}%)',
                      f"거래당 평균 순수익률 {sum(t['net_bps'] for t in trades)/len(trades)/100:+.3f}%",
                      '청산 사유: ' + ' · '.join(f'{REASONS.get(k,k)} {v}건' for k,v in Counter(t['reason'] for t in trades).items())]
        else:
            lines.append('완료 거래 표본 없음 · 손익·승률 산출 불가')
        if data['excluded']:
            lines.append('평가 제외: ' + ' · '.join(f'{REASONS.get(k,k)} {v}건' for k,v in data['excluded'].items()))
        lines += [f"비용 가정(편도): 수수료 {number(p['fee_bps'])/100:g}% + 추가 슬리피지 {number(p['slippage_bps'])/100:g}%",
                  f"지연 가정 {number(p['latency_ms']):g}ms · 최대 보유 {number(p['max_hold_ms'])/1000:g}초",
                  '호가 스프레드·모형 비용 반영. 실제 체결 실적이 아닙니다.',
                  '독립 실험 합계이며 계좌 수익률·잔고·최대낙폭은 산출하지 않습니다.',
                  '실시간 모의투자 /fast_report · 본 재생 실험의 수익성 미검증']
        return '\n'.join(lines), keyboard()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return (title + '\n완료 결과를 확인할 수 없습니다. 저장 파일 누락·형식을 점검해야 합니다.\n'
                '미확인 자료를 0원 수익이나 정상 실적으로 표시하지 않습니다.'), keyboard()
