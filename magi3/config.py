from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Config:
    mode: str
    live_enabled: bool
    kill_switch: bool
    max_order_krw: int
    max_total_exposure_krw: int
    max_daily_loss_krw: int

    @classmethod
    def from_env(cls):
        mode=os.getenv("MAGI3_MODE","DRY_RUN").upper()
        if mode not in {"DRY_RUN","SHADOW","LIVE"}:
            raise ValueError("MAGI3_MODE must be DRY_RUN, SHADOW, or LIVE")
        return cls(mode,os.getenv("MAGI3_LIVE_ENABLED","0")=="1",os.getenv("MAGI3_KILL_SWITCH","0")=="1",int(os.getenv("MAGI3_MAX_ORDER_KRW","30000")),int(os.getenv("MAGI3_MAX_TOTAL_EXPOSURE_KRW","30000")),int(os.getenv("MAGI3_MAX_DAILY_LOSS_KRW","10000")))

    @property
    def can_submit_live(self):
        return self.mode=="LIVE" and self.live_enabled and not self.kill_switch
