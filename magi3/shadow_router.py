from dataclasses import dataclass
from .shadow import simulate_market
@dataclass(frozen=True)
class ShadowCandidate:
    venue:str; market:str; fill:object; total_cost_bps:float

def rank_candidates(candidates):
    return sorted(candidates,key=lambda x:(not x.fill.complete,x.total_cost_bps))

def upbit_like_candidate(venue,market,book,side,quote_amount,fee_rate):
    fill=simulate_market(book,side,quote_amount,fee_rate)
    return ShadowCandidate(venue,market,fill,fill.slippage_bps+fee_rate*10000)
