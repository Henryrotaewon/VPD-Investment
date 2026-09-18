import os,time,hmac,hashlib,requests
from urllib.parse import urlencode
from .base import require_credentials
BASE="https://api.binance.com"
class BinanceAdapter:
    venue="binance"
    def __init__(self,session=None):self.http=session or requests.Session()
    def orderbook(self,symbol,limit=100):
        r=self.http.get(BASE+"/api/v3/depth",params={"symbol":symbol,"limit":limit},timeout=5);r.raise_for_status();return r.json()
    def account(self):
        require_credentials("BINANCE_API_KEY","BINANCE_SECRET_KEY")
        p={"timestamp":int(time.time()*1000)};q=urlencode(p)
        p["signature"]=hmac.new(os.environ["BINANCE_SECRET_KEY"].encode(),q.encode(),hashlib.sha256).hexdigest()
        r=self.http.get(BASE+"/api/v3/account",params=p,headers={"X-MBX-APIKEY":os.environ["BINANCE_API_KEY"]},timeout=5);r.raise_for_status();return r.json()
