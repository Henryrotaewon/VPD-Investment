# MAGI3 Foundation

Execution/portfolio service. This phase is intentionally non-trading.

Defaults:
- MAGI3_MODE=DRY_RUN
- MAGI3_LIVE_ENABLED=0
- MAGI3_KILL_SWITCH=0
- MAGI3_MAX_ORDER_KRW=30000
- MAGI3_MAX_TOTAL_EXPOSURE_KRW=30000
- MAGI3_MAX_DAILY_LOSS_KRW=10000

Run: `python -m magi3.runner`

The Upbit adapter currently permits public orderbook reads only. No withdrawal path will be implemented.
