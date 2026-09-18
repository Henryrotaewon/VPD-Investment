import argparse,json
from pathlib import Path
from .portfolio import Position,VenuePortfolio
from .report import build_report,render_text

def load(path):
    x=json.loads(Path(path).read_text(encoding="utf-8"));out=[]
    for v in x.get("venues",[]):
        ps=[Position(p["venue"],p["asset"],float(p["qty"]),float(p["avg_price"]),float(p["mark_price"]),p.get("quote","KRW"),tuple(p.get("strategies",[])),p.get("signal_id")) for p in v.get("positions",[])]
        out.append(VenuePortfolio(v["venue"],float(v.get("cash_value",0)),ps))
    return out
def main():
    a=argparse.ArgumentParser();a.add_argument("--input",required=True);a.add_argument("--json",action="store_true");z=a.parse_args()
    r=build_report(load(z.input));print(json.dumps(r,ensure_ascii=False,indent=2) if z.json else render_text(r))
if __name__=="__main__":main()
