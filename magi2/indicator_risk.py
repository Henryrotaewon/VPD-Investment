"""Independent, cost-adjusted loss limit for the hourly PAPER strategy."""
import math

from magi2.indicator_protection import breakeven_price, net_return

VERSION = 1
MAX_LOSS_PCT = 6.0
POLL_SECONDS = 10
QUOTE_MAX_AGE_MS = 60_000
REASON = 'INDEPENDENT_NET_STOP_6'


def stop_price(pos, state):
    return breakeven_price(pos, state) * (1 - MAX_LOSS_PCT / 100)


def position_key(pos):
    return (pos['entry_ms'], pos['cost'], pos['qty'])


def valid_quote(quote, pos, stamp):
    """Only fresh observed quotes after this entry may create a new exit."""
    try:
        price, ts = float(quote['price']), float(quote['ts'])
        return (math.isfinite(price) and price > 0 and math.isfinite(ts) and
                pos['entry_ms'] <= ts <= stamp and stamp - ts <= QUOTE_MAX_AGE_MS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def breached(pos, state, price):
    return net_return(pos, state, price) <= -MAX_LOSS_PCT + 1e-10
