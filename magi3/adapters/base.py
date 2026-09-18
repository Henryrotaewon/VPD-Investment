import os
PLACEHOLDER_VALUES={"","1","changeme","placeholder"}
def usable_secret(name):
    v=os.getenv(name,"").strip()
    return bool(v) and v.lower() not in PLACEHOLDER_VALUES
class CredentialsNotReady(RuntimeError): pass
def require_credentials(*names):
    missing=[n for n in names if not usable_secret(n)]
    if missing: raise CredentialsNotReady("CREDENTIALS_NOT_READY:"+",".join(missing))
