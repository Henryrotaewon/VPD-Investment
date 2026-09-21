# FAST live paper accounts

User request, 2026-09-21: each observed exchange starts with KRW 1,000,000 of
virtual capital; buy and sell captured candidates within 5–10 minutes, prioritize
market liquidation by ten minutes, and report results daily like VPD.

## Active selection from 2026-09-21

`fast-volume-accel-v2` implements the requested turnover/price acceleration rule.
Every minute, take a fresh venue price snapshot, rank positive five-minute gains,
and inspect up to 20 strongest symbols with public one-minute candles. The old
five-watch occupancy and rank-jump ordering do not block these inspections.
An initial five-minute price-history warmup is visible after restart.

Use eleven consecutive completed candles with a three-second completion delay:
the extra close supplies the beginning price for two adjacent five-minute windows.
Require recent five-minute return >=1%, strictly greater than the preceding
five-minute return, and executed quote turnover >=2x the preceding five minutes.
Quote turnover is the sum of actual trade value, not cancellable order-book size,
buyer-side volume, a count of orders, or the change in a rolling 24-hour total.
Kraken turnover is candle VWAP times base volume. Missing minutes, zero prior
turnover, invalid values, forming candles and failed requests are rejected explicitly.

There is **no spread, minimum tick, 30-second breakout, 2% chase or 70% buyer-share
filter in v2**. Fresh positive uncrossed quotes are still required. Observed spread
remains in evidence and in executable ask/bid paper costs. Thresholds are initial
research assumptions, not optimized parameters or a profitability claim.

One signal per venue/symbol per ten minutes; the cooldown survives restart. All
qualified signals are recorded and offered to the paper ledger before notification
throttling (existing three/hour, ten-minute gap, same-asset/hour notification rules).
Detailed probes and reasons, including the explicit 20-probe budget exclusion list,
are stored in the audit. This is a bounded scanner, not complete tick-level coverage.
At most 50 signals per venue retain independent ten-minute quote observations;
observation overflow is marked unavailable and does not cancel a paper position.
Paper exits run on their existing independent workers.

New paper rows preserve `strategy_version` and their entry-spread policy. v2 removes
the fixed entry-spread ceiling; historical/pending v1 trades retain the old 15bps
policy. Accounts, seed timestamp, balances, fills and deadlines are not reset.
Recent trades show v1/v2; daily reports separate completed-trade net P&L by version.
These before/after cohorts are **not simultaneous randomized A/B results**.

## Paper execution baseline

- Policy `fast-paper-5m-10m-v1`. Four independent accounts: Upbit KRW, Bithumb KRW,
  Binance USDT and Kraken USD. No real exchange orders or account credentials.
- Each new qualified FAST v2 signal may reserve KRW 200,000
  including buy fees. Maximum five pending/open positions per venue, one per
  symbol, 60-second same-symbol cooldown after complete exit. Cash cannot be
  reused while reserved or invested. Seed money is never reset on restart/day change.
- Notification suppression does not suppress paper trades. Old observations are
  never replayed as live fills; entry expires ten seconds after detection.
- Market entry uses a new public book requested after a 250ms decision delay,
  ask VWAP and at most 10% of displayed depth. Reject slow (>1.5s),
  stale (>3s), invalid or insufficient books. No synthetic full entry on missing depth.
  Zero-quantity levels are omitted before best-price/depth checks; negative or
  nonfinite quantities and books with no available side remain invalid.
- Request market exit five minutes after entry. Absolute deadline is **ten
  minutes after signal detection**, including entry delay. The deadline is
  persisted, independent of scanner observations and the notification queue.
  At/after the deadline, prioritize liquidation and block new entry for that venue.
- Exit uses bid depth, including partial fills, and retries any remainder. It
  never invents fills from stale or absent books or repeatedly consumes an identical
  unchanged bid snapshot. Feed outages, service downtime and insufficient liquidity
  can prevent completion by ten minutes; pending and late exits are explicitly
  counted. Resuming after restart immediately evaluates the original deadline.
- This initial five-minute baseline does **not** activate the separate research
  take-profit/profit-protection policy in `trade_policy.json`. A/B/C exit-policy
  comparisons require separate ledgers on the same future signals.

## Costs and currencies

Per-side fee assumptions are Upbit 5bps, Bithumb 4bps, Binance 10bps, Kraken 40bps;
these are configurable-in-code simulation assumptions, not verified user account
rates or current fee promises. Each side adds 5bps slippage on top of observed
book VWAP. Spread is already represented by ask/bid and is not subtracted twice.

KRW venues start immediately with KRW 1,000,000. Binance uses the initial public
Upbit USDT/KRW price. Kraken USD uses that price divided by Kraken USDT/USD.
Store source, time and the conversion once; all later KRW presentation uses that
fixed conversion. This isolates trading P&L from FX movement; it is not a claim
that USD and USDT are equal. Missing initial conversion prevents entries and is
shown as funding pending. Native cash accounting is retained throughout.

## Storage and reporting

`/data/magi2/fast_paper.sqlite3` contains accounts, slot reservations/trades,
immutable modeled fills, original deadlines, marks and the daily delivery outbox.
SQLite WAL + FULL synchronous transactions cover each fill and its cash mutation.
The FAST evidence database and existing VPD/MAGI1/MAGI3 state are unchanged.
Only position marks are overwritten; no all-market raw tape is added.

- `/fast_report` / `📊 FAST 모의검증 결과`: ten most recent paper investments,
  newest entry first, one row per venue/symbol with buy→sell times in KST and
  fee/slippage-inclusive net return. Open/pending positions show their status
  instead of an invented final return; skipped signals are not investments.
- `/fast_balance`: current cash, liquidation-mark equity, seed-relative cumulative
  return, today's realized P&L/fees, entries, complete exits, skipped entries,
  open/overdue positions and late closes, separately by venue.
- `/fast_orders`: most recent ten actual paper entries, exits, costs, net
  returns, hold durations and late/pending exit status.
- `/fast_daily`: preceding KST calendar day's realized results. At **09:00 KST**,
  Telegram automatically receives the prior day report. Restart catch-up begins
  with the first trading date. A durable outbox retries failures; a crash after
  Telegram accepts but before local acknowledgment can duplicate a daily message.
- `/fast_replay`: the earlier offline replay-file viewer, kept separate.
- `/fast_compare`: the existing fee-excluded five-minute signal diagnostic.

Daily realized P&L uses sale-fill timestamps and proportional cost basis, including
partial exits. Complete-trade win counts use final close date and full-trade P&L.
The daily message labels balances as **current**, not historical end-of-day equity.
Version rows sum full-trade P&L for trades completed on that date; this can differ
from the sale-fill-date realized P&L when a trade has partial exits across midnight.
Pending reservations remain cash; stale marks withhold whole-account equity instead
of converting missing prices to zero. No real fill accuracy or profitability claim.

## Market data references

- [Upbit order books](https://docs.upbit.com/kr/reference/list-orderbooks)
- [Upbit minute candles](https://docs.upbit.com/kr/reference/list-candles-minutes)
- [Kraken minute OHLC and VWAP/volume](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
- Bithumb uses the existing public `/v1/orderbook?markets=<symbol>` format
  (`market`, `orderbook_units`, `bid_price`, `bid_size`, `ask_price`, `ask_size`).
- [Binance public order book](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)
- [Kraken public depth](https://docs.kraken.com/api-reference/market-data/get-order-book)

These references define public data formats, not a simulation's execution guarantee.
