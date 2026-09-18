from datetime import datetime,timezone
from .portfolio import strategy_attribution

def build_report(venues,mode="SHADOW",fx_note="values must already be normalized to report currency"):
    total=sum(v.total for v in venues);pnl=sum(v.pnl for v in venues)
    invested=sum(v.invested for v in venues);cash=sum(v.cash_value for v in venues)
    return {"generated_at":datetime.now(timezone.utc).isoformat(),"mode":mode,"currency":"KRW",
      "total_summary":{"total_asset":total,"cash":cash,"invested":invested,"unrealized_pnl":pnl,
        "return_pct":pnl/(invested-pnl)*100 if invested-pnl else 0},
      "strategy_summary":strategy_attribution(venues),
      "venues":[{"venue":v.venue,"total_asset":v.total,"cash":v.cash_value,"invested":v.invested,"pnl":v.pnl,
        "positions":[{"asset":p.asset,"qty":p.qty,"avg_price":p.avg_price,"mark_price":p.mark_price,
          "value":p.value,"pnl":p.pnl,"return_pct":p.return_pct,"pick_basis":list(p.strategies),
          "signal_id":p.signal_id} for p in v.positions]} for v in venues],
      "valuation_note":fx_note}

def render_text(r):
    t=r["total_summary"];lines=[f'MAGI3 ASSET REPORT [{r["mode"]}]',f'TOTAL {t["total_asset"]:,.0f} KRW | CASH {t["cash"]:,.0f} | INVESTED {t["invested"]:,.0f} | PNL {t["unrealized_pnl"]:+,.0f}']
    lines+=["","[STRATEGY ATTRIBUTION]"]
    for k,v in sorted(r["strategy_summary"].items()):lines.append(f'{k}: {v["return_pct"]:+.2f}% | PNL {v["pnl"]:+,.0f} | positions {v["positions"]}')
    for v in r["venues"]:
        lines+=["",f'[{v["venue"].upper()}] total {v["total_asset"]:,.0f} | cash {v["cash"]:,.0f} | pnl {v["pnl"]:+,.0f}']
        for p in v["positions"]:lines.append(f'- {p["asset"]}: {p["value"]:,.0f} | {p["return_pct"]:+.2f}% | PICK={"+".join(p["pick_basis"]) or "UNATTRIBUTED"}')
    return "\n".join(lines)
