# FAST-DERIVATIVES / FAST-BEAR forward paper experiment

Created 2026-10-09. Each account starts with KRW 3,000,000 in separate versioned
SQLite ledgers under `/data/magi2/fast-models-v1`. No exchange order API, credentials,
borrowing or live capital is connected. Existing FAST remains stopped. Existing
VPD, indicator and Bollinger accounting and raw MAGI1 schemas are preserved.

## Data audit

The production MAGI1 feed diagnostics at 2026-10-09 14:37 KST showed healthy
spot trade/book reception for Binance, Kraken, Upbit and Bithumb, across
BTC/ETH/XRP/SOL/NEAR. Existing Bybit linear ticker context is not a futures
execution feed. There was no Binance/Kraken futures-depth and settled-funding
ledger for historical cash-and-carry replay. Earlier spot propagation results
cannot establish derivatives arbitrage profitability.

The existing independent FAST control and early-entry experiments were stopped:
control approximately -1.84%, early-entry approximately -4.09% in the inspected
production log. These are different historical cohorts, not new-model backtests.
MAGI1 Drive archival currently returns `invalid_grant`; local data is retained.
This release does not change historical archives or their OAuth credentials.

## Feed contract

| Market | Public collection | Accounting use |
|---|---|---|
| Upbit | KRW spot books, KRW-USDT conversion book, all-KRW WebSocket trades/books | Spot pairs and rebound account |
| Bithumb | KRW spot depth for five common assets | Spot side of cross-venue pairs |
| Binance | Spot and USD-M linear perpetual depth, exchangeInfo filters, premiumIndex, historical funding | USDT spot/short/pairs |
| Kraken | USD spot depth/AssetPairs; PF linear perpetual instruments, orderbook/tickers/funding history | USD spot/short/pairs |
| FX | Upbit KRW-USDT and Kraken USDT-USD bid/ask | USDT and USD remain separate currencies |

No Upbit/Bithumb futures feed is invented. Inverse and dated futures are excluded
from version 1. Symbol identity includes venue, product type, base and quote.
Kraken PF contract size must be one, with quantity precision from metadata.
Unavailable symbols/endpoints are recorded, not backfilled with another price.

New derivatives feeds run in the existing public-data worker in MAGI2; MAGI1's
protected Flow process, storage and export formats are unchanged. Derivatives
polling is asynchronous every ~10 seconds with six-way bounded book requests,
8-second request timeout, 5-minute cooldown after 418/429/451. New entries require
book age <=15s, request RTT <=3s, leg receive-time skew <=3s, metadata <=2h,
FX available and funding observations <=3m. REST books lacking snapshot exchange
time explicitly use receipt time; this is not subsecond latency arbitrage.

Funding refresh is once a minute; instrument discovery hourly. Only compact top
three book levels are sampled to SQLite each minute and retained seven days.
Trades and daily statistics are persistent. API payloads are held only in memory.

## FAST-DERIVATIVES

One KRW 3m account, with six virtual pre-funded wallets of KRW 500,000 each:
Upbit spot KRW, Bithumb spot KRW, Binance spot USDT, Binance perp USDT,
Kraken spot USD, Kraken perp USD. FX conversion happens once when fresh bid/ask
quotes are available. Foreign cash is subsequently revalued in KRW; its FX PnL
is part of the account return. There are no instantaneous transfers or shared
margin across venues. Unavailable wallets remain idle. Slot capital ceiling is
KRW 300,000, with at most ten positions; wallet capacity may bind sooner.

Two tagged entry hypotheses share this same capital:

- `SHORT`: BTC completed 4h close below EMA20 and EMA20 falling; asset's latest
  completed Upbit 5m close breaks the previous six complete candle lows. The
  signal candle must close after account start. Initial short stop 1.5%, take
  profit 3%, maximum four hours. This deliberately uses a published cross-market
  spot signal; it is not labelled futures sell-aggressor detection.
- `BASIS`: buy spot and sell the same base quantity of a linear perpetual.
  Require 120 previously observed spread samples, positive gross spread, net
  edge >=30bp after four fees/slippage and 20bp FX/model buffer, and deviation
  above prior mean by max(2 standard deviations, 30bp). Current funding must
  be nonnegative. This is a convergence hypothesis, not locked-in arbitrage.
  Exit when observed closing spread returns to prior mean, net capital return
  hits +0.5%/-0.5%, or 24 hours elapse.

Each short reserves 100% notional collateral plus 20% buffer, separately from
spot purchase capital. Quantity is rounded to common lot step. At entry every
leg's minimum order and visible depth must pass. Duplicate same-asset positions
and re-entry within a 09:00 KST day are disallowed. Pending orders reserve wallet
cash and expire in 30s. Fills need subsequent observations, price cap 0.3%, and
complete visible depth for both legs; cost-adjusted pair edge is checked again.
Exit intent also precedes the observations used for simulated fills.

Paired fills use a synchronous public-depth model: either both legs pass or the
pair is skipped. It does not simulate real queue position, independent exchange
partial fills, transfer restrictions, order acknowledgments or counterparty
failure. Before a real execution project, a separate asynchronous leg-risk and
liquidation replay is required. No real-trading readiness is claimed here.

Kraken funding is the published absolute USD/base-unit/hour rate prorated over
actual holding time, split at UTC hourly boundaries. Binance funding is booked
only from historical payment rows if the position spans the payment timestamp.
A persisted cursor prevents duplicate funding after restart. Missing settlements
make NAV incomplete; no positive expected funding is booked as realized profit.
NAV remains incomplete until funding is available; requested protective exits are
recorded from available quotes even during a funding gap and settle conservatively
when the missing amount can be reconciled.

Margin buffer exits use mark price; if a price gap exceeds posted collateral,
wallet debt is recorded and new entries stop. This remains a simplified full
collateral paper model, not an exact exchange liquidation-engine replica.

Assumed taker fees (not account-specific quoted rates): Upbit 0.05%, Bithumb
0.25%, Binance spot 0.10%, Kraken spot 0.40%, both perps 0.05%, per side.
Depth VWAP plus 0.05% adverse slippage per side. FX bid/ask spread is explicit.
Fees and assumptions are versioned; discount coupons are not assumed.

## FAST-BEAR

Separate KRW 3m cash account; Upbit KRW symbols share the existing public
WebSocket stream. The existing causal paper-fill implementation is reused.
A newly connected full five-minute candle is required after historical warmup;
historical signals never cause backdated entries. Missing/no-trade candles are
not interpolated into signal eligibility.

BTC 4h regime uses the same completed-candle definition above, refreshed every
five minutes. It is a shorter-horizon filter than MAGI1's general market view.

B: RSI(14, Wilder) crosses from <=30 to >30 and the completed 5m close recovers
above rolling one-hour VWAP, with buy-aggressor notional >=60% and >=20 trades.
The next complete 5m bar must hold VWAP, have a non-decreasing low, buy share
>=60% and >=20 trades. Signal-time low/max 1.5% protection, 0.3% entry cap,
10-second pending lifetime. No same-day repurchase, maximum ten slots.

C: if a completed bar closes below VWAP with buy share <40%, request an exit.
At +2% gross movement raise protection to +1%; exit at +4%, initial protection
or one-hour time limit. Protection is a trigger, not a guaranteed fill price.
Data gaps use the first fresh executable book to unwind, with reason recorded.
Fees and extra slippage are 0.05% each per side, plus actual visible spread.

## Comparable reporting

- Both models use 09:00 KST trading days; neither capital nor positions reset daily.
- Net winning completed positions / all completed positions; zero-PnL trades
  remain in the denominator. Partial exits aggregate into one position; two-leg
  pairs count once. SHORT and BASIS closed PnL are also shown separately.
- Return = marked total account equity / original KRW 3m - 1, including foreign
  cash FX exposure, both legs, collateral, fees and settled/accrued funding.
- Running maximum drawdown from equity observations (10-second target), preserving
  initial capital and high-water mark across restarts. Intraday drawdown and
  lifetime drawdown are distinct. Stale marks never increase the high-water mark.
- Data gaps are marked explicitly. Daily return requires a valid previous boundary
  observation within one minute; a multi-day gap is not reported as one-day return.
- Telegram: strategy validation or FAST menu -> FAST-DERIVATIVES / FAST-BEAR.
  `/fast_models`, `/fast_derivatives`, `/fast_bear`, `/fast_model_daily`,
  `/fast_model_data`. All views are read-only. Daily comparisons persist without
  needing an external scheduled call; no unsolicited new notification is added.

## Validation

New tests exercise capital conservation, shared-wallet reservations, base-unit
rounding, stale/next-observation fills, margin return, two-leg PnL, funding signs
and restart idempotence, absent funding, hourly proration, independent capital,
09:00 day boundaries, intraday MDD and invalid observations, rebound confirmation
and old FAST pause isolation. MAGI1 raw schema/regression suite is required.

Public specification references:
- https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- https://docs.kraken.com/api-reference/instrument-details/get-instruments
- https://docs.kraken.com/api-reference/market-data/get-tickers
- https://docs.kraken.com/api-reference/historical-funding-rates/historical-funding-rates
- https://support.kraken.com/articles/4844359082772-linear-multi-collateral-derivatives-contract-specifications
