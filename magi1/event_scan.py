from .event_report import publish_event

def publish_new_events(storage,since_ms,end_ms,min_score=70):
    rows=storage.query("derived_signal",since_ms,end_ms);out=[]
    for x in rows:
        if float(x.get("score",0))>=min_score:out.append(publish_event(storage,x))
    return out
