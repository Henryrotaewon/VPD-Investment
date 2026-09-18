# MAGI3 Shadow runtime and read-only accounts

2026-09-18 implementation. **No live order runtime exists.** `LIVE` mode or `MAGI3_LIVE_ENABLED=1` makes the new runner refuse startup. Code completion and unit tests do not prove that exchange credentials are valid.

| Area | Implemented | Boundary |
|---|---|---|
| Signal path | MAGI1 VPD saved scan → MAGI2 durable Shadow outbox → MAGI3 private polling | MAGI1 FAST/Whale observations are not promoted directly to trades |
| Shadow execution | Public depth IOC buy, partial fill/remainder cancellation, holding deadline exit, partial exit retry | Virtual KRW cash; no exchange orders or real fills |
| Recovery | SQLite WAL, atomic signal/order/fill/position/cash transactions, stable IDs, pending recovery, persisted deadlines, single-process file lock | Persistent Railway volume required; no live order reconciliation |
| Risk | Per-order and total cost-basis exposure including entry fees; virtual cash, realized KST daily-loss limit; kill switch blocks new entries | Not mark-to-market drawdown control; exits still run; config changes require restart |
| Reporting | Shadow cash/equity/fees/realized/unrealized PnL and original strategy tags; equal-split multi-tag attribution | Midpoint valuation, future exit fees excluded from unrealized PnL; no queue/latency impact model |
| Accounts | Upbit/Bithumb JWT, Binance signed account, Kraken BalanceEx; locked balances included | Read-only spot API scope; Earn, futures and subaccounts not separately aggregated |
| Real valuation | Venue books, KRW and market-implied USDT/USD conversions | Missing quotes/venues remain unknown; partial sums are labeled; no fictional fallback |
| Real cost/PICK | Exchange KRW average acquisition cost where supplied | Unknown cost, realized PnL and strategy basis remain unknown/UNATTRIBUTED |

## Shadow experiment policy

`VPD_SHADOW_V1` uses the newest Upbit KRW morning/evening scan no older than 12 hours. MAGI2 creates one intent per snapshot/asset with VPD ≥75, a 10,000 KRW budget and 300-second maximum holding time. Intent lifetime is 120 seconds from first observation by MAGI2; restart never refreshes it. VPD score is a heuristic, not a probability. This is a plumbing/forward-observation experiment, not the existing PAPER strategy or a profitability-approved strategy. FAST/WAVE/Whale automated entries are not enabled.

Initial virtual cash is 100,000 KRW and is initialized only once. Default fee model is 10 bps per side (`MAGI3_SHADOW_FEE_BPS`), not a claim about actual account commissions. Default exposure limit 30,000 KRW includes entry fees, so three 10,000 KRW buys cannot all fit. Rejected signals are not repeatedly reissued. An expired/illiquid signal may produce no trade. Deadline exits can be delayed when public quotes are unavailable; `/execution` exposes pending exits.

## Operation

Run `python -m magi3.runner` with:

- `MAGI3_MODE=SHADOW`, `MAGI3_LIVE_ENABLED=0`
- `MAGI3_DATA_DIR=/data/magi3` on an attached persistent volume
- `MAGI2_SHADOW_SIGNALS_URL=http://vpd-investment.railway.internal:8082/signals`
- `MAGI_SERVICE_TOKEN` (at least 32 characters, same private transport token on MAGI2)
- `MAGI3_HTTP_PORT=8083` (private IPv6 bind by default)
- Existing risk limits and exchange API keys in Railway variables only

MAGI2 needs `MAGI2_SHADOW_BRIDGE_ENABLED=1`, `MAGI2_SHADOW_PORT=8082`, `MAGI3_SERVICE_URL=http://magi3-execution.railway.internal:8083`, and the same service token. Its outbox lives under the existing MAGI2 volume, separately from PAPER state. Account reads run separately from the execution loop with independent HTTP sessions.

Private authenticated GET endpoints: `/status`, `/accounts`, `/shadow`, `/orders`; no HTTP mutation routes. Logs contain operational status, not account balances or secrets. Ledger and real balance snapshots remain on the private volume, never GitHub.

Telegram: `/help`, `/menu`, `/assets` (real read-only), `/shadow` (virtual), `/orders` (virtual fills), `/execution` or legacy `/magi3` (runtime status), `/report` (existing PAPER). Korean buttons and unprefixed commands are supported.

Placeholders such as `1` are rejected before network access. Use exchange read-only balance permissions and a dedicated Kraken key to avoid cross-process nonce collisions. Four real account connections are only confirmed when runtime reports successful authenticated reads. A credential failure is not a zero balance.

## Verification

`python -m unittest discover -s magi3/tests -v`

`python -m unittest discover -s tests -v`

Tests cover atomic rollback, restart deduplication, partial buy/exit, deadline recovery, fees and realized PnL, limits, source expiry, credential placeholders, account gaps and unknown cost, official Kraken signature vector, private endpoint authorization and Telegram routing.

Official signing/account references:

- [Bithumb JWT](https://apidocs.bithumb.com/docs/%EC%9D%B8%EC%A6%9D-%ED%86%A0%ED%81%B0-%EC%83%9D%EC%84%B1%ED%95%98%EA%B8%B0)
- [Kraken REST authentication](https://docs.kraken.com/exchange/guides/rest/authentication)
- [Kraken BalanceEx](https://docs.kraken.com/api-reference/account-data/get-extended-balance)
- [Binance account](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)

The legacy `report_cli` sample remains synthetic and is never used by `/assets`. Legacy Upbit `order_test` and `submit` are not called by this runtime; `submit` remains blocked.
