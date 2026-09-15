"""MAGI1 research configuration.

Research only. No execution/order endpoints belong in MAGI1 v0.1.
"""

VENUES = {
    "binance": {"region": "GLOBAL", "quote_preferences": ["USDT", "USD"]},
    "bybit": {"region": "GLOBAL", "quote_preferences": ["USDT", "USD"]},
    "kraken": {"region": "GLOBAL", "quote_preferences": ["USD", "USDT"]},
    "upbit": {"region": "KOREA", "quote_preferences": ["KRW"]},
    "bithumb": {"region": "KOREA", "quote_preferences": ["KRW"]},
    "coinone": {"region": "KOREA", "quote_preferences": ["KRW"]},
}

UNIVERSE_SIZE = 5

# Store enough depth to calculate robust liquidity/imbalance features while
# keeping the first experiment lightweight.
BOOK_DEPTH = 10

MICRO_WINDOWS_MS = (100, 250, 500, 1_000, 5_000, 10_000)
MESO_WINDOWS_SEC = (30, 60, 300, 600)
FORWARD_WINDOWS_SEC = (10, 30, 60, 300, 1_800, 3_600)

STATE_FORMATION = "FORMATION"
STATE_GLOBAL_CONSENSUS = "GLOBAL_CONSENSUS"
STATE_KOREA_EARLY_RECEPTION = "KOREA_EARLY_RECEPTION"
STATE_PROPAGATION = "PROPAGATION"
STATE_EXHAUSTION = "EXHAUSTION"
STATE_NORMALIZATION = "NORMALIZATION"

DIRECTIONS = ("BUY", "SELL")

# These are observation/storage defaults, not trading thresholds.
RAW_FLUSH_INTERVAL_SEC = 5
HEARTBEAT_TIMEOUT_SEC = 30
RECONNECT_BACKOFF_SEC = (1, 2, 5, 10, 30)

DEFAULT_DATA_DIR = "/data/magi1"
UNIVERSE_REFRESH_SEC = 86_400
STALE_FEED_SEC = 45
REPORT_HOUR_KST = 7
FORMATION_RETURN_BPS = 4.0
CONSENSUS_WINDOW_MS = 10_000
KOREA_RECEPTION_WINDOW_MS = 30_000
NORMALIZATION_WINDOW_MS = 300_000
PRICE_NON_REACTION_BPS = 2.0

# Flow Formation Score weights/thresholds are intentionally absent in v0.1.
# They must be estimated from collected data rather than chosen to fit anecdotes.
