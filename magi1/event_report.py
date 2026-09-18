"""Event reports are persisted records; notification transport stays outside MAGI1."""
def publish_event(storage,signal):
    row={"event_ts_ms":signal["event_ts_ms"],"asset":signal["asset"],"event_type":signal["signal_type"],
      "direction":signal["direction"],"score":signal["score"],"confidence":signal["confidence"],
      "evidence":signal["evidence"],"report_version":"event-v1"}
    storage.append("event_report",row);return row
