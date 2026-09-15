from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence


@dataclass(frozen=True)
class TradeEvent:
    venue: str
    symbol: str
    base: str
    quote: str
    exchange_ts_ms: Optional[int]
    received_ts_ms: int
    price: float
    quantity: float
    side: Optional[str] = None
    trade_id: Optional[str] = None


@dataclass(frozen=True)
class BookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class BookEvent:
    venue: str
    symbol: str
    base: str
    quote: str
    exchange_ts_ms: Optional[int]
    received_ts_ms: int
    bids: Sequence[BookLevel] = field(default_factory=tuple)
    asks: Sequence[BookLevel] = field(default_factory=tuple)
    sequence: Optional[str] = None

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0


@dataclass(frozen=True)
class FlowStateEvent:
    asset: str
    direction: str
    state: str
    event_ts_ms: int
    global_venues_confirmed: int
    korea_venues_confirmed: int
    origin_venue: Optional[str] = None
    origin_confidence: Optional[float] = None
    flow_formation_score: Optional[float] = None
    reference_price_reaction_pct: Optional[float] = None
    sample_version: str = "magi1-v0.2"
    horizon: str = "MICRO"


@dataclass(frozen=True)
class ShockOriginCandidate:
    source: str
    provider: str
    asset: str
    direction: str
    event_ts_ms: int
    received_ts_ms: int
    category: str
    amount: Optional[float] = None
    tx_hash: Optional[str] = None
    confidence: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    standalone_signal: bool = False


@dataclass(frozen=True)
class PropagationMeasurement:
    shock_id: str
    asset: str
    direction: str
    origin_venue: str
    follower_venue: str
    origin_ts_ms: int
    reception_ts_ms: Optional[int]
    origin_confidence: float
    reception_probability: float
    lag_ms: Optional[int]
    sensitivity: Optional[float]
    usable_lead_time_ms: Optional[int]
    sample_count: int


@dataclass(frozen=True)
class EvaluationRecord:
    shock_id: str
    cohort: str
    horizon_sec: int
    forward_return: Optional[float]
    mfe: Optional[float]
    mae: Optional[float]
    false_shock: Optional[bool]
