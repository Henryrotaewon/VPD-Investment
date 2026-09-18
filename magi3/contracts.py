from dataclasses import asdict
from .models import StrategySignal
REQUIRED=("signal_id","created_ts_ms","asset","side","strategy","confidence","expected_move_bps","max_holding_sec")
def parse_strategy_signal(x):
    missing=[k for k in REQUIRED if k not in x]
    if missing:raise ValueError("MISSING_SIGNAL_FIELDS:"+",".join(missing))
    venues=tuple(x.get("preferred_venues") or ())
    return StrategySignal(x["signal_id"],int(x["created_ts_ms"]),str(x["asset"]),str(x["side"]).upper(),str(x["strategy"]),float(x["confidence"]),float(x["expected_move_bps"]),int(x["max_holding_sec"]),venues,x.get("regime"),x.get("exit_policy"),x.get("metadata") or {})
def signal_dict(s):return asdict(s)
