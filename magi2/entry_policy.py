POLICY = 'ENTRY_SCORE_V1_1'

def _num(row, name, default=0.0):
    try:
        value = row.get(name)
        return default if value in (None, '') else float(value)
    except (TypeError, ValueError):
        return default

def _clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, value))

def assess_entry(row, cfg):
    """Score whether a TOP10 row is suitable for a *new* PAPER entry.

    VPD ranking finds strong candidates. EntryScore answers a different
    question: is the current price/risk structure suitable for entering now?
    """
    ecfg = cfg.get('entry', {})
    vpd = _num(row, 'VPD')
    velocity = _num(row, 'VPDVelocity')
    momentum = str(row.get('momentum', '')).strip()
    one_d = _num(row, '1D%')
    one_h = _num(row, '1H%')
    giveback = _num(row, 'Giveback%p', 999.0)
    today_value = _num(row, 'TodayValue/10')
    intra = _num(row, 'IntraAccel')
    risk = str(row.get('DistributionRisk', 'CLEAR')).upper()
    spike = bool(row.get('SpikeCollapse', False))

    blocked = []
    if spike:
        blocked.append('SPIKE_COLLAPSE')
    if risk == 'HIGH':
        blocked.append('DISTRIBUTION_HIGH')
    if giveback >= float(ecfg.get('hard_max_giveback_pct_point', 25.0)):
        blocked.append('GIVEBACK_HARD')
    if vpd < float(ecfg.get('min_vpd', 70.0)):
        blocked.append('VPD_LOW')
    if one_d >= float(ecfg.get('hard_max_1d_pct', 20.0)):
        blocked.append('PRICE_OVERHEAT')
    if one_d <= float(ecfg.get('hard_min_1d_pct', -12.0)):
        blocked.append('PRICE_BREAKDOWN')

    # 100-point timing score. The score deliberately rewards signal strength
    # while penalising late entries after a large run-up/giveback.
    vpd_score = _clamp((vpd - 70.0) / 30.0) * 25.0
    velocity_score = _clamp(velocity / 40.0) * 20.0
    momentum_score = {'↑↑': 15.0, '↑': 12.0, '→': 5.0}.get(momentum, 0.0)
    activity_score = (
        _clamp(today_value / 10.0) * 8.0
        + _clamp(intra / 12.0) * 7.0
    )

    timing_score = 15.0
    if one_d >= 12.0:
        timing_score -= 8.0
    elif one_d >= 8.0:
        timing_score -= 5.0
    elif one_d >= 5.0:
        timing_score -= 2.0
    elif one_d <= -8.0:
        timing_score -= 8.0
    elif one_d <= -5.0:
        timing_score -= 5.0
    elif one_d <= -2.0:
        timing_score -= 2.0
    if one_h <= -1.0:
        timing_score -= 4.0
    timing_score = max(0.0, timing_score)

    # Giveback penalty starts only after 6%p and increases gradually.
    # 0~6%p: no penalty, 6~9: -2, 9~12: -4,
    # 12~15: -6, 15~20: -8, 20~25: -10, 25%p+: hard block.
    if giveback <= 6.0:
        giveback_score = 10.0
    elif giveback <= 9.0:
        giveback_score = 8.0
    elif giveback <= 12.0:
        giveback_score = 6.0
    elif giveback <= 15.0:
        giveback_score = 4.0
    elif giveback <= 20.0:
        giveback_score = 2.0
    else:
        giveback_score = 0.0

    score = vpd_score + velocity_score + momentum_score + activity_score + timing_score + giveback_score
    if risk == 'WATCH':
        score -= float(ecfg.get('watch_penalty', 10.0))
    if str(row.get('trigger', '')).upper() == 'A':
        score += 3.0
    if bool(row.get('Rocket', False)):
        score += 2.0
    if bool(row.get('NEW_TOP10', False)):
        score += 1.0
    score = round(max(0.0, min(100.0, score)), 2)

    min_score = float(ecfg.get('min_entry_score', 55.0))
    eligible = not blocked and score >= min_score
    reasons = []
    if blocked:
        reasons.extend(blocked)
    if not blocked and score < min_score:
        reasons.append(f'ENTRY_SCORE<{min_score:g}')
    if not reasons:
        reasons.append('PASS')

    return {
        'eligible': eligible,
        'score': score,
        'reasons': reasons,
        'components': {
            'vpd': round(vpd_score, 2),
            'velocity': round(velocity_score, 2),
            'momentum': round(momentum_score, 2),
            'activity': round(activity_score, 2),
            'timing': round(timing_score, 2),
            'giveback': round(giveback_score, 2),
            'distribution_risk': risk,
        },
    }

def rank_entry_candidates(rows, cfg):
    assessed = []
    for index, row in enumerate(rows):
        result = assess_entry(row, cfg)
        assessed.append((row, result, index))
    assessed.sort(key=lambda x: (not x[1]['eligible'], -x[1]['score'], x[2]))
    return assessed
