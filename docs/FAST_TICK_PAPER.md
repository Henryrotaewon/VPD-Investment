# Active experiment: FAST v4 (2026-09-21 user reset)

User-authorized change: buy a limit at the latest observed current trade price (no one-tick discount), then sell at the current price plus one valid tick and repeat until the original signal +10 minutes. Current-price limit orders are not guaranteed immediate fills. Existing depth/queue evidence, fees, residual market exit, 20만원 slots and five concurrent symbols remain.

Selection is now solely current public ticker price / the observation nearest five minutes ago −1 >=5%. Poll every 60 seconds, allow at most ±15 seconds around the five-minute baseline and record actual duration. Reject unavailable/stale prices; do not interpolate. First startup needs approximately five minutes of observations. There is no volume-ratio, acceleration or spread eligibility filter. At most 20 leading qualifying symbols receive quote validation per scan; capital and same-symbol cooldown still limit paper admission.

`fast_reset.reset_once` runs before workers using exact two-file allowlist: `fast_paper.sqlite3`, `fast_evidence.sqlite3` and their WAL/SHM companions. It deletes old operational FAST captures, trades, fills, daily report queue and four virtual accounts, including outstanding paper positions. The new ledger starts each venue at KRW 1,000,000 equivalent, overseas FX set once from public prices. A durable `fast-current-fivepct-v4-20260921` marker prevents repetition on restart. VPD, MAGI1 and MAGI3 files are untouched. Previously exported research documents are not operational history and are unchanged.

v4 selection: `fast-price-rise-v4`; execution: `fast-current-cycle-v4`; Telegram menu v17. The latest-ten, capture and comparison views recognize v4. No live exchange orders are sent.

Validation: 108 FAST tests plus 19 Telegram tests pass, including 5% boundary, missing baseline, current-price entry, following sell, fixed deadline, scoped reset, all four seeds, marker idempotence and v4 views. The description below records the superseded v3 experiment and its unchanged execution evidence/fee assumptions.

---

# FAST v3: 10-minute tick cycles (PAPER)

The previous execution baseline bought once at the displayed ask and sold after five minutes. Its results do not evaluate the user's repeated-limit strategy. Selection remains `fast-volume-accel-v2`; new execution is `fast-tick-cycle-v3`.

## Execution contract

- Reserve one KRW 200,000 equivalent slot out of each venue's original KRW 1,000,000 account; at most five concurrent symbols. Preserve original seed date, cash, FX and all history. Profits/losses are not reset.
- At detection, request a BUY limit one valid exchange tick below the latest observed trade price.
- Once the purchased portion meets the venue's minimum sell size, cancel any unfilled BUY remainder and submit a SELL limit one valid tick above the then-current trade price. If the portion is too small, continue accumulating the BUY. Existing SELL partials remain on that order.
- Once the inventory is sold, repeat BUY/SELL using the same reserved slot. Each new BUY is capped at the lesser of the original slot and remaining session cash. Do not reprice an outstanding order merely because the market moves.
- The deadline is exactly signal timestamp + 600,000 ms; cycles and process restarts never extend it. An independent 250 ms deadline worker cancels all limit orders and requests market liquidation of residual inventory. Quote latency, outages, minimum order restrictions or insufficient depth can delay actual completion; record the delay and remaining inventory instead of inventing a fill.
- Existing legacy positions retain their stored five-minute exit policy. New sessions cannot enter through the old market-buy method.
- This code only uses public GET endpoints. It contains no authenticated exchange order submission.

## Fill evidence and limitations

Submission latency is at least 250 ms. Initial activation requires a fresh post-latency depth snapshot. The order must be within visible depth, and displayed quantity at its price becomes the initial queue ahead. Subsequent fills require new opposite-aggressor trade prints at the limit price or better; consume queue ahead before the paper order. A price touch alone is not a fill. Cancellations ahead receive no queue credit. A newly marketable limit uses at most 10% of visible opposite depth within its limit, charged at the taker rate.

Trade IDs are treated as unique, not numerically ordered. Exchange time and IDs jointly prevent duplicate processing. A nonoverlapping REST trade page triggers a data-gap event, no retroactive fills, and fresh depth resynchronization. Each next order has a new activation/cursor, so it cannot reuse preceding cycle prints. Order state, counters, fills, accounts and deadlines persist in the original SQLite database; events are append-only.

This is a conservative public-data simulation, not reconstruction of the actual exchange queue. REST polling is nominally once per second per venue, serial across up to five sessions; network time increases the interval. Hidden liquidity, cancellations, price-time priority and the impact of the hypothetical order cannot be known exactly. High activity can exceed 500/1000 recent-trade pages and lead to missed fills. These constraints mean past five/ten-minute price observations cannot be truthfully replayed as tick-strategy profits.

Forced liquidation consumes fresh bid depth with an extra 0.05% price penalty. Unchanged depth cannot be consumed twice. Invalid/absent quotes leave inventory pending. Minimum-order dust remains explicitly pending rather than being written off as sold. No new sessions are accepted in a venue while overdue inventory remains.

## Rules and costs

| Venue | Price rule | Quantity/minimum assumption | Maker / taker fee |
|---|---|---|---|
| Upbit | KRW official grid, including band crossings | 1e-8 base step; KRW 5,000 minimum | 0.05% / 0.05% |
| Bithumb | Official trade-kit KRW grid | Conservative 1e-4 base step; KRW 5,000 minimum | 0.04% / 0.04% |
| Binance | Per-symbol exchangeInfo PRICE_FILTER | LOT_SIZE, MARKET_LOT_SIZE and minimum NOTIONAL/MIN_NOTIONAL | 0.10% / 0.10% |
| Kraken | Per-pair AssetPairs tick_size | lot_decimals, ordermin and costmin | 0.40% / 0.80% |

Fees are explicit simulation assumptions, not authenticated account quotations or promotional rates. Kraken v3 uses the published July 2026 base tier; historical v1/v2 rows retain their original 0.4% assumption. KRW quantity/minimum rules are conservative paper assumptions where account-specific metadata is unavailable. Binance percent-price filters and account-level limits are not fully reproduced. Compare execution cohorts separately; do not attribute differences solely to order type when fee assumptions differ.

2026-09-21 read-only public checks confirmed Upbit ZETA tick 0.1, Bithumb ZETA depth increments 0.01 in the 10–100 band, Binance BTCUSDT tick 0.01/lot 0.00001/minimum 5 USDT, and Kraken ZETAUSD tick 0.0001/lot 0.00001/minimum 150 ZETA and 0.5 USD. An attempted Binance ZETAUSDT sample returned HTTP 400; BTCUSDT validated the schema. This probe does not alter the scanner universe.

Sources:
- [Upbit KRW policy](https://docs.upbit.com/kr/docs/krw-market-info)
- [Upbit orderbook instruments](https://docs.upbit.com/kr/reference/list-orderbook-instruments)
- [Bithumb official trade kit](https://github.com/bithumb-official/bithumb-ai-trade-kit/blob/2b6304261c81f48256e63b0588db2ca1c44df52f/skills/bithumb-trade/references/order-commands.md)
- [Bithumb public trades](https://apidocs.bithumb.com/reference/체결-내역-조회)
- [Binance filters](https://developers.binance.com/en/docs/products/spot/filters)
- [Kraken pair rules](https://docs.kraken.com/api-reference/market-data/get-tradable-asset-pairs)
- [Kraken fees](https://www.kraken.com/features/fee-schedule)
- [Kraken July 2026 fee changes](https://support.kraken.com/articles/cross-platform-fee-tier-changes)

## Reports and accounting

`/fast_report` shows the latest ten accepted sessions, including unfilled sessions, signal/end times, completed limit round trips and final market exit. v3 net return divides total realized session P&L by the original allocated slot, never cumulative repeated-buy turnover. Legacy rows retain entry-cost returns and a five-minute label. `/fast_orders` adds first-buy/last-sell times, pending order price/quantity, fees and forced-exit P&L. The existing daily 09:00 KST report separates v3 from prior cohorts.

For closed v3 sessions, gross P&L minus fees minus extra forced-exit slippage equals net P&L. Forced-exit P&L is a subset of total net P&L, not an additional amount. Normal round-trip counts exclude forced exits. Unfilled sessions do not count as completed investments or wins. Account cash reconciles to initial funding plus fill cashflows; reservation is logical and does not prematurely debit cash.

## Validation

`test_fast_tick.py` covers band boundaries, queue consumption, wrong-side trades, submission delay, duplicate and nonmonotonic IDs, gaps/restarts, partial buy handoff, partial sells, two cycles and fee/cash conservation, no timer reset, deadline cancellation and market exit, unchanged-depth protection, dust failures, no market-entry fallback, original seed/history preservation, report denominators, and public API shapes. Existing FAST, Telegram and regime tests remain regression gates.
