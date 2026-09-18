"""Stable additive MAGI1 -> MAGI2/MAGI3 market-intelligence contract."""
def market_intelligence(storage,start_ms,end_ms):
    signals=storage.query("derived_signal",start_ms,end_ms)
    states=storage.query("flow_state",start_ms,end_ms)
    return {"contract":"magi1-market-intelligence-v1","window":{"start_ms":start_ms,"end_ms":end_ms},
      "signals":signals,"flow_states":states,
      "semantics":{"FAST":"market acceleration evidence","WHALE":"large-transfer evidence; not ownership/causality",
        "FLOW":"existing formation/propagation state"}}
