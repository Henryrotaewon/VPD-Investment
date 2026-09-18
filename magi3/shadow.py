from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class ShadowFill:
    side:str
    requested_quote:float
    filled_base:float
    filled_quote:float
    avg_price:float
    fee_quote:float
    slippage_bps:float
    complete:bool

def simulate_market(orderbook,side,quote_amount,fee_rate=0.0005):
    """Walk visible Upbit orderbook. BUY consumes asks; SELL consumes bids.

    quote_amount is KRW notional. This is a conservative visible-book model:
    no queue-position assumption and no future liquidity assumption.
    """
    if quote_amount<=0:raise ValueError("quote_amount must be positive")
    units=(orderbook[0] if isinstance(orderbook,list) else orderbook).get("orderbook_units",[])
    levels=[(float(x["ask_price"]),float(x["ask_size"])) for x in units] if side=="BUY" else [(float(x["bid_price"]),float(x["bid_size"])) for x in units]
    if not levels:return ShadowFill(side,quote_amount,0,0,0,0,0,False)
    ref=levels[0][0];remaining=float(quote_amount);base=quote=0.0
    for price,size in levels:
        capacity=price*size
        take=min(remaining,capacity)
        if take<=0:break
        base+=take/price;quote+=take;remaining-=take
        if remaining<=1e-9:break
    avg=quote/base if base else 0
    slip=((avg/ref)-1)*10000 if side=="BUY" and ref else ((ref/avg)-1)*10000 if avg else 0
    return ShadowFill(side,float(quote_amount),base,quote,avg,quote*fee_rate,slip,remaining<=1e-9)
