from dataclasses import dataclass,field
from collections import defaultdict

@dataclass
class Position:
    venue:str; asset:str; qty:float; avg_price:float; mark_price:float
    quote:str="KRW"; strategies:tuple[str,...]=(); signal_id:str|None=None
    @property
    def cost(self):return self.qty*self.avg_price
    @property
    def value(self):return self.qty*self.mark_price
    @property
    def pnl(self):return self.value-self.cost
    @property
    def return_pct(self):return self.pnl/self.cost*100 if self.cost else 0

@dataclass
class VenuePortfolio:
    venue:str; cash_value:float=0; positions:list[Position]=field(default_factory=list)
    @property
    def invested(self):return sum(x.value for x in self.positions)
    @property
    def total(self):return self.cash_value+self.invested
    @property
    def pnl(self):return sum(x.pnl for x in self.positions)

def strategy_attribution(venues):
    a=defaultdict(lambda:{"invested":0.0,"pnl":0.0,"positions":0})
    for v in venues:
        for p in v.positions:
            tags=p.strategies or ("UNATTRIBUTED",)
            share=1/len(tags)
            for tag in tags:
                a[tag]["invested"]+=p.cost*share;a[tag]["pnl"]+=p.pnl*share;a[tag]["positions"]+=1
    for x in a.values():x["return_pct"]=x["pnl"]/x["invested"]*100 if x["invested"] else 0
    return dict(a)
