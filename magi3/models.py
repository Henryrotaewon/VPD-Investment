from dataclasses import dataclass,field
from typing import Any
import uuid

@dataclass(frozen=True)
class StrategySignal:
    signal_id:str; created_ts_ms:int; asset:str; side:str; strategy:str
    confidence:float; expected_move_bps:float; max_holding_sec:int
    preferred_venues:tuple[str,...]=(); regime:str|None=None; exit_policy:str|None=None
    metadata:dict[str,Any]=field(default_factory=dict)

@dataclass(frozen=True)
class OrderIntent:
    signal_id:str; venue:str; market:str; side:str; order_type:str; amount_krw:int
    client_order_id:str=field(default_factory=lambda:uuid.uuid4().hex)
