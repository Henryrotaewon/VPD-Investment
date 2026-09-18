from dataclasses import dataclass
from .config import Config
from .models import OrderIntent

@dataclass(frozen=True)
class RiskSnapshot:
    total_exposure_krw:int=0
    daily_realized_pnl_krw:int=0

def check(intent:OrderIntent,snapshot:RiskSnapshot,cfg:Config):
    reasons=[]
    if cfg.kill_switch:reasons.append("KILL_SWITCH")
    if intent.amount_krw<=0:reasons.append("INVALID_AMOUNT")
    if intent.amount_krw>cfg.max_order_krw:reasons.append("MAX_ORDER")
    if snapshot.total_exposure_krw+intent.amount_krw>cfg.max_total_exposure_krw:reasons.append("MAX_EXPOSURE")
    if snapshot.daily_realized_pnl_krw<=-cfg.max_daily_loss_krw:reasons.append("DAILY_LOSS_LIMIT")
    if cfg.mode=="LIVE" and not cfg.live_enabled:reasons.append("LIVE_NOT_ARMED")
    return not reasons,reasons
