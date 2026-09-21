"""Exchange price/quantity grids for public-data-only limit-order simulation."""
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING


def dec(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('INVALID_DECIMAL')
    return number


def floor_qty(value, step):
    step = dec(step)
    if step <= 0: raise ValueError('INVALID_QUANTITY_STEP')
    return float((dec(value) / step).to_integral_value(rounding=ROUND_FLOOR) * step)


UPBIT_GRID = [('1000000','1000'),('500000','500'),('100000','100'),
              ('50000','50'),('10000','10'),('5000','5'),('100','1'),
              ('10','.1'),('1','.01'),('.1','.001'),('.01','.0001'),
              ('.001','.00001'),('.0001','.000001'),('.00001','.0000001'),('0','.00000001')]
# Bithumb's official trade-kit price table, revision 2b630426 (2026-09-21 lookup).
BITHUMB_GRID = UPBIT_GRID[:7] + [('10','.01'),('1','.001'),('0','.0001')]


def tick_at(price, rules):
    if dec(price) <= 0: raise ValueError('INVALID_REFERENCE_PRICE')
    if rules.get('grid'):
        for threshold, step in rules['grid']:
            if dec(price) >= dec(threshold): return dec(step)
        raise ValueError('UNSUPPORTED_PRICE_BAND')
    step = dec(rules['tick'])
    if step <= 0: raise ValueError('INVALID_TICK')
    return step


def adjacent(price, side, rules):
    """Adjacent valid price, including downward crossings of price bands."""
    p = dec(price)
    candidates = []
    grids = rules.get('grid') or [('0', rules['tick'])]
    for i, (lower, step) in enumerate(grids):
        low, tick = dec(lower), dec(step)
        high = dec(grids[i-1][0]) if i else Decimal('Infinity')
        if side == 'BUY':
            candidate = ((p / tick).to_integral_value(rounding=ROUND_CEILING)-1)*tick
            if candidate >= high: candidate = high-tick
            if low <= candidate < p and candidate < high and candidate > 0:
                candidates.append(candidate)
        elif side == 'SELL':
            candidate = max(low, ((p/tick).to_integral_value(rounding=ROUND_FLOOR)+1)*tick)
            if p < candidate < high: candidates.append(candidate)
        else: raise ValueError('INVALID_SIDE')
    if not candidates: raise ValueError('NO_ADJACENT_PRICE')
    result = max(candidates) if side == 'BUY' else min(candidates)
    if result < dec(rules.get('min_price',0)) or (rules.get('max_price') and result > dec(rules['max_price'])):
        raise ValueError('PRICE_OUTSIDE_LIMITS')
    return float(result)


def valid_size(price, qty, rules, market=False):
    prefix = 'market_' if market else ''
    minimum = float(rules.get(prefix+'min_qty', rules['min_qty']))
    maximum = float(rules.get(prefix+'max_qty', rules.get('max_qty',float('inf'))))
    notional = float(rules.get(prefix+'min_notional',rules['min_notional']))
    return qty > 0 and qty+1e-12 >= minimum and qty <= maximum and price*qty+1e-8 >= notional
