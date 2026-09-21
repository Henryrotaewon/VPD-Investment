# FAST target/stop paper: 07:30 sessions and confirmed controls

Updated 2026-09-21 by user request. The main keyboard contains one FAST entry,
`FAST 모의투자`. Its four actions are `포착 리스트`, `모의투자 결과`,
`일괄정리 및 포착정지`, and `포착 및 매매 시작`. The latter two require a
chat/actor-bound, one-use confirmation with a 60-second lifetime, including
legacy commands and callbacks. Reads never initiate trading.

The trading/reporting day runs from 07:30 KST through the following 07:30.
Capture lists show the latest capture per venue/symbol, newest first, with
venue, asset and capture time only. Same-day sale exclusion uses this boundary.
Day changes never liquidate holdings, reset equity, or resume a paused strategy.

Each of four paper venues is seeded with KRW 1,000,000 equivalent. Each venue
has five slots capped at KRW 200,000 **including entry fees**. A smaller free
cash balance can fund a smaller slot if exchange minimum size is met. Pending
entries reserve cash. Proceeds become available to subsequent fresh signals;
there is no top-up of existing positions or replay of stale signals.

Stop atomically blocks captures and entries, cancels pending entries and requests
market exits. Exit workers continue until completed; unavailable liquidity is
reported as pending, never as a fabricated fill. Pause state survives restart.
Start is refused while liquidation remains pending. After start, fresh
five-minute observations are built; scans begun before the transition cannot
emit captures or buy. Existing financial history remains intact across controls.

Reports mirror VPD: initial KRW 4,000,000, remaining purchase cost, cash, total
equity, cumulative percentage and amount, followed by current holdings/pending
entries. Each holding shows venue-symbol, capture time, average purchase price,
current bid, and unrealized net P&L including remaining purchase costs and
estimated taker exit fee/slippage. Stale marks withhold valuation. Foreign
quotes use the initial fixed conversion rate. Closed history remains accessible
by the legacy detail command but is excluded from the holdings page.

The user explicitly requested deletion of all existing FAST captures and trades.
One-time reset marker is `fast-session0730-v6-20260921`, deleting only FAST paper
and evidence SQLite databases plus their WAL/SHM files before workers start.
This releases their storage and starts four new seeds; VPD and other services
are untouched. Subsequent restarts preserve the new records. Initial capture
and paper execution are enabled, continuing the requested paper experiment.

The +12%/-6% execution rules below remain in force. Earlier discussion of price
caps/IOC was a proposal and is not implemented by this menu/session change.

## Original execution mechanics (current)

User-authorized replacement of execution only; detector stays fast-price-rise-v4 (5-minute +5%, 60-second polling). No live exchange orders.

- Four venue accounts, KRW 1,000,000 equivalent each; 200,000 per position, five positions per venue.
- Immediate paper market entry against a fresh public ask book after 250 ms latency; depth, quantity filters, fees and 5 bp additional slippage apply. Entry evidence expires after 10 seconds; missing/depleted books do not invent fills.
- Average execution price (before fees, including simulated impact) fixes a 112% target, rounded UP to a valid exchange tick. Sell limit is persisted in the same transaction as the entry.
- Best executable bid at/below 94% of the fixed entry average triggers cancellation and market liquidation. Gaps, fees and impact can make net loss exceed 6%.
- No timed exit, no repeated buys. Target fills use the existing conservative queue/aggressor-tape model. A target outside visible depth rests until depth becomes observable; no fictitious queue/fill.
- Each sale, including a partial sale, blocks that venue/symbol until the next 07:30 KST. The block is derived from durable fills and survives process restart. Next-session fresh signals may enter; holdings continue to block duplicate entry.
- Confirmed stop/start and 07:30 reports follow the controls above.
- Daily summaries use the 07:30 trading day. Initial capital stays fixed at KRW 4,000,000.


Validation covers target rounding, market entry evidence, fixed target/no time exit, profitable target exit, stop boundary, fee/slippage reconciliation, restart/day rollover, partial market exit recovery, four-venue clear, tape-independent stop, reports and authenticated command/button routing.

## Verification

Automated checks cover 07:30 boundaries, all four seeds, five-slot reservations,
partial free cash, fee-inclusive slot caps, stop during an in-flight entry/scan,
restart while paused, pending liquidation recovery, report reconciliation,
confirmation/cancel/expiry/replay/actor checks, and one-time exact-file reset.
