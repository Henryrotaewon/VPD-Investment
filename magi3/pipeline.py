"""MAGI3 consumes StrategySignal; MAGI1 is never imported here."""
from .contracts import parse_strategy_signal
def consume(raw):
    s=parse_strategy_signal(raw)
    tags=tuple(raw.get("strategy_tags") or [x for x in s.strategy.split("+") if x])
    return s,tags
