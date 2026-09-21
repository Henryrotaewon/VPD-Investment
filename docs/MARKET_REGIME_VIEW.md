# On-demand market regime view

`🧭 시장 국면`, `/regime`, and `nav:regime` request a short, read-only Telegram
response. There is no scheduled collection, regime alert, persistence, cached
fallback, strategy selection, allocation output, or connection to an order client.
A dedicated single worker keeps the public-data request off PAPER and FAST workers;
repeat clicks during a running request do not enqueue extra work.

## Data and interpretation

The view requests BTC/USD and ETH/USD daily OHLC from Kraken's public endpoint:
https://docs.kraken.com/api-reference/market-data/get-ohlc-data

Kraken's final row is an unfinished candle. Only fully closed UTC days are used,
with a three-minute publication grace period. Both assets must have the same 400
contiguous completed days, ending at the latest eligible close. Missing, stale,
duplicate, nonpositive, nonfinite or invalid timestamps/prices withhold judgment;
there is no forward fill or substitution of another price source. The response
shows the daily close and request time in KST. A daily regime is not an intraday
FAST entry signal or a macro leading forecast.

- Direction: mean BTC/ETH 90-day log return, positive = up, otherwise down.
- Risk: sample standard deviation of the equal-weight daily log returns over
  30 days, annualized by sqrt(365).
- High volatility: above the 65th percentile of the preceding 252 daily volatility
  observations, excluding the classified day's observation.
- Four states: 상승·저변동 / 상승·고변동 / 하락·저변동 / 하락·고변동.
- A state is confirmed after two consecutive completed days. A conflicting latest
  observation is shown as a pending transition alongside the last confirmed state.
  Conflicting BTC/ETH directions are explicitly identified when not in transition.

These price-only conventions match the research classifier's trend/risk definitions,
but this view uses the latest completed Kraken candle, not the research's delayed
Coin Metrics historical snapshot. It does not inherit backtest returns or claim
predictive accuracy. Macro rates, futures, indices and issuance events are not yet
connected. The historical research strategy/weight experiment remains separate.

## Validation

`tests/test_market_regime.py` covers incomplete/stale/missing/invalid data, all four
states, previous-only thresholds, two-day transitions, public request parameters,
read-only authorized routing, duplicate request suppression, and worker errors.
Run it with `python -m unittest discover -s tests -p test_market_regime.py`.
Existing Telegram and FAST tests verify the adjacent operational paths.
