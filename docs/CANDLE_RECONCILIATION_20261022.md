# Double Bollinger / FAST-BEAR native candle reconciliation

## Problem and evidence

The 2026-10-22 production logs repeatedly showed most Upbit symbols in
`INCOMPLETE_CANDLES`. At 22:55 KST Bollinger had 8 ready / 298 symbols; at
22:56 BEAR had 17 ready / 298. These totals alone did not distinguish native
no-trade intervals from collector gaps.

Code inspection and regression tests identified a recovery defect: when a
completed live bar was absent, or indicator history contained a missing bar,
the account changed its status but did not enqueue REST reconciliation. A
recoverable gap could consequently block Bollinger for its 348-slot / 29-hour
window. BEAR also reused the 348-bar bootstrap although its existing feature
calculation only reads the last 60 bars.

Upbit's minute-candle API omits intervals with no trades:
https://docs.upbit.com/kr/reference/list-candles-minutes

## Changes

- Enqueue missing native slots for bounded REST recovery. Preserve live
  continuity, the two-second freshness gate, existing accounts and trade rules.
- Audit the required history once; subsequent reconciliation stops after the
  page covering the oldest unknown gap or recent reconnect bridge. At most two
  200-row pages, one request per second per existing worker.
- BEAR's bootstrap now stops at 60 slots (normally one page). Its RSI/VWAP
  calculation and existing minimum 30-bar validation are unchanged.
- Classify no-trade intervals only inside the timestamp span proven by a
  validated native API page. An empty response is not proof of no trading.
  Confirmed no-trade slots remain absent and do not cause repeated repair of
  the same hole. No previous-close or synthetic OHLC is created.
- Preserve the generation guard against a reconnect during REST work. Repaired
  history cannot replay a past signal; a new fully observed live bar must close.
- Expose repair queue, native backfill count, classified reasons and five bounded
  symbol samples in status logs; show the reason counts in read-only views.

## Validation

48 focused tests passed (reconciliation, Bollinger paper/report and FAST models),
including recovery after a missed live bar, true no-trade exclusion, old holes,
one-page BEAR/bridge requests, no historical orders and in-flight reconnects.
The existing CI suite remains the deployment gate. Production readiness must be
evaluated after initial bootstrap and a subsequent complete live five-minute
bar; zero no-trade exclusions is neither required nor expected.

No balance, position, fill or daily-performance reset is part of this change.
