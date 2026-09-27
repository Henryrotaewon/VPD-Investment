# Double Bollinger + CCI paper strategy v1

This independent MAGI2 paper account tests a candidate rule; it does not claim
predictive power, a backtested edge, or a profitable live result. No private
exchange client or order credentials enter the public-feed worker.

## Rules

| Item | Rule |
| --- | --- |
| Universe | Upbit KRW spot markets from the public market list |
| Account | Separate KRW 3,000,000 account, at most 10 positions including pending entries; available cash divided by remaining slots |
| Candles | Completed 5-minute candles, exchange timestamps |
| Bands | SMA of closes, population SD; BB(20,2) and BB(60,2) |
| CCI | 10 typical prices `(high+low+close)/3`; mean absolute deviation; constant 0.015; zero deviation gives CCI=0 |
| Squeeze | Current BB60 width `(upper-lower)/middle*100` <= linear 20th percentile of the preceding 288 widths, excluding the current width |
| Watch | Squeeze arms the next six bars (30 minutes); another squeeze refreshes it; the breakout bar cannot arm itself |
| Entry | Previous close <= previous BB20 upper, current close > current upper; BB20 width grows; close > BB60 middle and middle does not fall; CCI>100; candle KRW turnover >= 1.5 times preceding 20-bar mean (signal bar excluded) |
| Fill | First eligible fresh quote after the decision, for up to 10 seconds; full entry budget must fit visible asks; slipped average price <= signal close * 1.003 |
| Initial stop | Minimum low of six completed bars including signal bar; reject if stop >= expected fill or `(fill-stop)/fill > 3%` |
| Stop exit | Fresh trade or best bid <= stop requests exit; subsequent fresh bid depth determines actual fill, including partial exits |
| Trend exit | Completed close < BB20 middle AND CCI<0; execute against subsequent fresh bid depth |
| Reentry | A new squeeze candle must start after the previous full exit; no blanket same-day ban |
| Costs | Each side: 0.05% fee plus 0.05% adverse slippage, in addition to observed spread/depth |

CCI need not cross +100 on the breakout candle. Upper-band touches and CCI+200
are not exits. FAST's ten-second flow exit, three-minute idle exit and
one-minute trailing stop do not apply. The stop-distance ceiling is a signal
filter, not a guaranteed maximum realized loss.

## Data and continuity

At least 348 contiguous completed candles are needed: 60 closes for the first
long band, 288 prior bandwidths and the current band. Missing/no-trade bars are
not interpolated and prevent a decision until the required history is complete.
The last 400 candles per market are retained in the new ledger.

A serial background worker loads at most two 200-candle REST pages per market,
with at least one second between requests. Only HTTP runs in a thread; SQLite
and trading decisions share the feed event loop. This work is additional to
the existing FAST baseline repair and is capped at one request per second.
Rate limits pause history loading while WebSocket monitoring continues.

Warmup and restart never replay historical signals. Only a live candle that
closes after history is loaded may arm a squeeze. A startup/reconnect partial
candle cannot trigger a signal: REST loading waits for that candle to close,
then includes it in history, bridging to the first fully observed live candle.
Loading does not discard an already continuously observed current live candle.
Every accepted unique trade contributes to its exchange-time
bucket, including out-of-order trades; time and trade ID determine open/close.
The observer rejects trades over two seconds late and invalidates the affected
indicator stream. Five-minute decisions wait 2.5 seconds for accepted boundary
trades and are skipped if processed more than ten seconds after the boundary.

Feed loss cancels pending entries and marks held positions uncertain. As in
the existing public paper account, the next fresh post-gap book liquidates
the affected holdings; the pre-gap price is never invented as a fill. The watch
is discarded and history reloaded. After exit, even an in-progress squeeze bar
that started before the exit cannot enable reentry.

## Persistence and results

`STATE_DIR/bollinger-paper/v1.sqlite3` holds the independent account, fills,
daily valuations, latest assessment per market and bounded candle cache.
It does not reset or migrate the VPD, FAST or indicator account. The public
worker remains keyless and observes the same public WebSocket subscription.

**전략검증 → 더블볼린저·CCI 결과** provides cumulative win rate/return first,
then daily win rate/return (09:00 KST trading-day boundary), with pagination.
Partial exits count once on final exit; fees and slippage are included. Returns
use account NAV including holdings. Missing valuations are explicit. Status
shows ready markets, warmup, insufficient candles, history retry, squeeze
watches, holdings and pending entries. Four strategy buttons use two rows.

## Verification

`python -m unittest tests.test_bollinger_paper -v` checks independent indicator
math, exclusion of the signal bar from reference statistics, warmup causality,
entry filters, six-bar expiry, execution price/depth/stop checks, exit rules,
partial fills, reentry, exchange timestamps, restart, results and FAST gate
isolation. The existing FAST, repair and performance test groups remain CI gates.
These are deterministic software tests, not strategy-return backtests.
