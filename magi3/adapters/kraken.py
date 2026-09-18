import os,requests
from .base import require_credentials
BASE="https://api.kraken.com"
class KrakenAdapter:
    venue="kraken"
    def __init__(self,session=None):self.http=session or requests.Session()
    def orderbook(self,pair,count=100):
        r=self.http.get(BASE+"/0/public/Depth",params={"pair":pair,"count":count},timeout=5);r.raise_for_status();return r.json()
    def balance(self):
        require_credentials("KRAKEN_API_KEY","KRAKEN_PRIVATE_KEY")
        raise RuntimeError("KRAKEN_PRIVATE_SIGNING_PENDING_REAL_KEY_VALIDATION")
