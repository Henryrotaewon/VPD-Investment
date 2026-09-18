from dataclasses import dataclass
@dataclass(frozen=True)
class VenueQuote:
    venue:str; market:str; best_bid:float; best_ask:float
    @property
    def mid(self):return (self.best_bid+self.best_ask)/2
    @property
    def spread_bps(self):return (self.best_ask-self.best_bid)/self.mid*10000 if self.mid else 0

def normalize_upbit_like(venue,market,book):
    b=book[0] if isinstance(book,list) else book;u=b["orderbook_units"][0]
    return VenueQuote(venue,market,float(u["bid_price"]),float(u["ask_price"]))

def normalize_binance(market,book):
    return VenueQuote("binance",market,float(book["bids"][0][0]),float(book["asks"][0][0]))

def normalize_kraken(market,book):
    data=next(iter(book["result"].values()))
    return VenueQuote("kraken",market,float(data["bids"][0][0]),float(data["asks"][0][0]))
