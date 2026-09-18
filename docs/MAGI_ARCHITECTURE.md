# MAGI Architecture v1

## Responsibility boundary
- MAGI1 = Market Intelligence: observe only. Wave, Flow, Rotation, Trend, Breadth, Exhaustion, on-chain/whale context. No buy/sell decision and no credentialed exchange client.
- MAGI2 = Strategy Lab: VPD, FAST, Wave/Trend/Rotation strategies, Venue Edge, PAPER/SHADOW evaluation. Produces strategy signals; never sends live orders.
- MAGI3 = Execution & Portfolio: risk gate, capital allocation, venue adapter, order/position state, P&L and investment reports.

## Hard routing rule
MAGI1 -> MAGI2 -> MAGI3. MAGI1 must never call MAGI3 directly.

## Safety invariants
1. MAGI3 defaults to DRY_RUN.
2. LIVE requires both MAGI3_MODE=LIVE and MAGI3_LIVE_ENABLED=1.
3. MAGI3_KILL_SWITCH=1 blocks new exposure.
4. Withdrawal functionality is out of scope and must not be implemented.
5. PAPER/SHADOW/LIVE state and ledgers are separate namespaces.
6. Every order intent has a unique client_order_id and is reconciled before retry.
7. Exchange credentials must come only from environment/secrets, never repository files or logs.

## StrategySignal contract
Required: signal_id, created_ts_ms, asset, side, strategy, confidence, expected_move_bps, max_holding_sec.
Optional: preferred_venues, regime, exit_policy, metadata.

MAGI3 treats this as an intent, not an order. Risk and venue checks may reject it.
