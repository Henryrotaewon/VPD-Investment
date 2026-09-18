import os,requests
from .base import require_credentials
BASE="https://api.bithumb.com"
class BithumbAdapter:
    venue="bithumb"
    def __init__(self,session=None):self.http=session or requests.Session()
    def orderbook(self,market):
        r=self.http.get(BASE+"/v1/orderbook",params={"markets":market},timeout=5);r.raise_for_status();return r.json()
    def accounts(self):
        require_credentials("BITHUMB_ACCESS_KEY","BITHUMB_SECRET_KEY")
        raise RuntimeError("BITHUMB_PRIVATE_SIGNING_PENDING_REAL_KEY_VALIDATION")
