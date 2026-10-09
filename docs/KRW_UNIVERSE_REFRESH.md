# KRW public universe refresh

The shared public observer previously fixed its symbol list at process startup.
New listings after startup therefore received no trade/book stream. This change
polls Upbit's public market list every 30 seconds (15-second request timeout).

New KRW symbols are appended to the shared warmup universe and subscribed in
incremental WebSocket groups of at most 100. Existing groups are not resubscribed
or reset. Connection attempts are paced 300 ms apart. Reconnects reuse each
group's own symbols and the existing exponential backoff.

Bollinger and FAST-BEAR receive new symbols through their existing connection
and warm-history paths. Their historical-data and first-complete-live-bar gates
are unchanged. If FAST repair is enabled, its queue also gains the new symbol.
No candle is fabricated and no earlier signal is replayed as a purchase.

An empty/malformed/failed market response leaves current subscriptions intact.
Markets absent from a later response remain observed, with `retained_absent`
reported; delisting liquidation/removal policy is outside this addition-only
change. Incremental groups can accumulate between process restarts, which
repack the current market list into 100-symbol groups.

The last universe is persisted in observer metadata; migration can read the
existing UNIVERSE event. Startup additions and runtime additions emit
FAST_UNIVERSE_ADDED. Regular status includes last successful discovery, listed
and registered counts, and first trade/book evidence for the ten latest additions.
Registration is not proof of a connected or fresh feed: `recent_feeds` reports
connection state and actual receive timestamps independently.

FAST remains USER_STOPPED. This changes public data coverage only; account
balances, existing ledgers, entry/exit thresholds and live-order controls are
untouched. Being observed does not make a new listing eligible for any strategy.
