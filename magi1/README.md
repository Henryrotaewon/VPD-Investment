# MAGI1 — Macro Market / Flow Formation Engine

MAGI1 is the macro-market intelligence layer for the VPD/MAGI research platform.

## Research thesis

The first objective is not latency arbitrage. MAGI1 observes how buying/selling pressure forms across multiple venues before price propagation is complete.

State model:

`FORMATION -> GLOBAL_CONSENSUS -> KOREA_EARLY_RECEPTION -> PROPAGATION -> EXHAUSTION/NORMALIZATION`

VPD remains an independent Upbit-KRW spot truth layer. Overseas data is not added to the VPD 100-point score.

## v0.1 experiment

Venues:
- Binance
- Bybit
- Kraken
- Upbit
- Bithumb
- Coinone

Universe:
- Compute the intersection of actively traded spot assets across all six venues.
- Rank eligible common assets by liquidity/market size using observable venue data.
- Start with the top five eligible assets.
- Do not hard-code coin names; persist the selected universe with timestamp and selection inputs.

Data layers:
- trades
- best bid/ask and spread
- L2 order-book features where supported
- exchange timestamp
- local receive timestamp
- aggressor/trade imbalance
- volume acceleration
- order-book imbalance / liquidity depletion
- rolling returns on micro and meso horizons

Time horizons:
- MICRO: 100 ms–10 s
- MESO: 10 s–10 min
- MACRO: 10 min–hours, joined later to VPD

Core research outputs:
- Flow Formation Score (research feature; weights must be learned/validated, not assumed)
- Origin Confidence
- Global Consensus state
- Korea Reception state
- propagation probability
- lag distribution
- response sensitivity
- false-follow rate
- MFE/MAE and forward returns

## Guardrails

- Research/PAPER only in v0.1.
- Do not submit orders or intentionally influence reference/oracle markets.
- Record BUY and SELL formations symmetrically.
- Preserve raw timestamps and raw market events so features can be replayed.
- Treat network latency as an explanatory variable, not the primary alpha thesis.
- No unverified metric is promoted to a production signal.

## Implemented modules

- `config.py` — venue/universe/research configuration
- `schema.py` — normalized event/state schemas
- `universe.py` — six-venue common-universe discovery and top-5 selection
- `collectors/` — exchange-specific public WebSocket adapters
- `normalizer.py` — normalized trades/order-book events
- `features.py` — micro/meso flow features
- `formation.py` — state transition and Flow Formation research logic
- `propagation.py` — origin/reception/lag statistics
- `storage.py` — append-only raw/research persistence
- `replay.py` — PAPER forward-return/MFE/MAE evaluation
- `vpd_join.py` — timestamp-safe VPD join, without modifying VPD score

Run with `python -m magi1.runner`. `MAGI1_MODE=COLLECT_ONLY` is mandatory and is also the default. Raw ticks are append-only files under `MAGI1_DATA_DIR`; they must not be committed to GitHub. The daily report is generated at 07:00 KST and reconstructs each chain in On-chain → Global → Derivatives → Korea → VPD → Price order.

On-chain events use the replaceable `OnChainProvider`/`EnrichmentProvider` interfaces. Public/raw sources are the initial boundary; Arkham, Nansen, Glassnode or another provider can enrich candidates later. Every on-chain candidate has `standalone_signal=False` by construction.

## Definition of done for v0.1

1. Six venues remain connected with reconnect/heartbeat handling.
2. Five common liquid assets are selected reproducibly.
3. Raw trade/order-book events can be replayed without look-ahead.
4. Formation and reception state transitions are logged for both directions.
5. Propagation statistics are generated from observed data with sample counts/confidence intervals.
6. VPD-only, Flow-only, VPD+Flow, and VPD+Flow+Price-Non-Reaction PAPER cohorts can be compared.
