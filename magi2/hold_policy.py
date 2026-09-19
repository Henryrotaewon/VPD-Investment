"""Pure VPD PAPER holding decisions; no I/O or orders.

TREND_PROFIT_V2 is an uncalibrated paper-policy candidate. Entry ranking,
trend continuation and protection of an existing profit are separate rules.
"""
import math

POLICY = 'TREND_PROFIT_V2'
LABELS = {
    'TREND': '추세 유지', 'PROFIT': '수익 보호 보유',
    'WEAKENING': '신호 약화', 'DATA_WAIT': '자료 확인 대기',
    'HARD_STOP': '손절', 'TAKE_PROFIT': '목표 익절',
    'PROFIT_PROTECTION': '수익 보호 청산',
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def profit_metrics(position, price, cfg):
    qty, cost, px = (number(x) for x in
                     (position.get('qty'), position.get('cost_krw'), price))
    if any(x is None or x <= 0 for x in (qty, cost, px)):
        return None
    peak = max(px, number(position.get('peak_price')) or px)
    fee = float(cfg.get('fee_rate', .0005))
    current = (qty * px * (1 - fee) / cost - 1) * 100
    peak_return = (qty * peak * (1 - fee) / cost - 1) * 100
    return {'observed_peak_price': peak, 'net_return_pct': current, 'peak_net_return_pct': peak_return,
            'profit_giveback_pct_point': max(0., peak_return - current)}


def protection(position, price, cfg):
    metrics = profit_metrics(position, price, cfg)
    if metrics is None:
        return {'kind': 'DATA_WAIT', 'reasons': ['현재 가격 또는 포지션 평가 자료 부족']}
    exits = cfg.get('exit', {})
    tp = float(position.get('target_profit_pct', exits.get('take_profit_pct', 12)))
    stop = float(position.get('stop_loss_pct', exits.get('hard_stop_pct', -6)))
    net = metrics['net_return_pct']
    kind = None
    if net <= stop:
        kind = 'HARD_STOP'
    elif net >= tp:
        kind = 'TAKE_PROFIT'
    elif exits.get('profit_protection_enabled', False):
        activate = float(exits.get('profit_protection_activation_pct', 6))
        giveback = float(exits.get('profit_protection_giveback_pct_point', 3))
        if metrics['peak_net_return_pct'] + 1e-9 >= activate and metrics['profit_giveback_pct_point'] + 1e-9 >= giveback:
            kind = 'PROFIT_PROTECTION'
    return dict(metrics, kind=kind, reasons=[])


def assess_hold(row, position, price, cfg):
    risk = protection(position, price, cfg)
    if risk['kind']:
        return dict(risk, policy=POLICY)
    momentum = str(row.get('momentum', '')).strip()
    today = number(row.get('TodayValue/10'))
    accel = number(row.get('IntraAccel'))
    if momentum not in {'↑', '↑↑', '→', '↓', '↓↓'} or today is None or accel is None:
        return dict(risk, policy=POLICY, kind='DATA_WAIT', reasons=['보유 추세 자료 누락 · 약화 횟수 미차감'])
    trend = momentum in {'↑', '↑↑'}
    hold = cfg.get('hold', {})
    activity = (today >= float(hold.get('min_today_value_ratio', 1)) or
                accel >= float(hold.get('min_intra_accel', 1)))
    reasons = [f'모멘텀 {momentum}', f'거래대금 {today:.2f}배 / 가속 {accel:.2f}배']
    if trend and activity:
        kind = 'TREND'
    elif trend and cfg['exit'].get('profit_protection_enabled',False) and risk['net_return_pct'] > 0 and risk['peak_net_return_pct'] >= float(cfg['exit'].get('profit_protection_activation_pct', 6)):
        kind = 'PROFIT'
        reasons.append('거래대금 약화 · 상승 추세와 기존 수익 보호 기준으로 보유')
    else:
        kind = 'WEAKENING'
        reasons.append('상승 추세 또는 거래대금 지지 부족')
    return dict(risk, policy=POLICY, kind=kind, reasons=reasons)


def decision_text(coin, row, assessment, suffix=''):
    score, rank = number(row.get('VPD')), number(row.get('Rank'))
    identity = f'{coin} · VPD {score:g}' if score is not None else coin
    if rank is not None:
        identity += f' ({rank:g}위)'
    label = LABELS.get(assessment['kind'], assessment['kind'])
    details = list(assessment.get('reasons', []))
    if 'net_return_pct' in assessment:
        details.append(f'순수익 {assessment["net_return_pct"]:+.2f}%')
    if assessment['kind'] in {'PROFIT', 'PROFIT_PROTECTION'}:
        details.append(f'관측 최고수익 대비 {assessment["profit_giveback_pct_point"]:.2f}%p 반납')
    if suffix:
        details.append(suffix)
    return f'{identity} · {label}\n  ' + ' · '.join(details)
