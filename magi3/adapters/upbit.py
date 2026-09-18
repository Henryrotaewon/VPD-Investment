"""Upbit adapter.

Private endpoints are read-only except order_test(), which calls Upbit's
non-executing /v1/orders/test endpoint.  There is deliberately no /v1/orders
live-submit implementation in this phase.
"""
import hashlib,os,uuid
from urllib.parse import urlencode,unquote
import jwt,requests
from .base import PLACEHOLDER_VALUES, CredentialsNotReady

BASE="https://api.upbit.com"

class UpbitAdapter:
    venue="upbit"
    def __init__(self,access_key=None,secret_key=None,session=None):
        self.access_key=access_key or os.getenv("UPBIT_ACCESS_KEY")
        self.secret_key=secret_key or os.getenv("UPBIT_SECRET_KEY")
        self.http=session or requests.Session()

    @staticmethod
    def _query(params):
        return unquote(urlencode(params or {},doseq=True))

    def _auth(self,params=None):
        if any(not k or k.strip().lower() in PLACEHOLDER_VALUES for k in (self.access_key,self.secret_key)):
            raise CredentialsNotReady("UPBIT_CREDENTIALS_NOT_READY")
        q=self._query(params)
        payload={"access_key":self.access_key,"nonce":str(uuid.uuid4())}
        if q:
            payload["query_hash"]=hashlib.sha512(q.encode("utf-8")).hexdigest()
            payload["query_hash_alg"]="SHA512"
        token=jwt.encode(payload,self.secret_key,algorithm="HS512")
        return {"Authorization":f"Bearer {token}","Accept":"application/json"}

    def orderbook(self,market):
        r=self.http.get(BASE+"/v1/orderbook",params={"markets":market},timeout=5)
        r.raise_for_status();return r.json()

    def ticker(self,markets):
        r=self.http.get(BASE+"/v1/ticker",params={"markets":",".join(markets)},timeout=8)
        r.raise_for_status();return r.json()

    def accounts(self):
        r=self.http.get(BASE+"/v1/accounts",headers=self._auth(),timeout=5)
        r.raise_for_status();return r.json()

    def order_chance(self,market):
        params={"market":market}
        q=self._query(params)
        r=self.http.get(BASE+"/v1/orders/chance?"+q,headers=self._auth(params),timeout=5)
        r.raise_for_status();return r.json()

    def order_test(self,market,side,ord_type,price=None,volume=None,identifier=None):
        body={"market":market,"side":side,"ord_type":ord_type}
        if volume is not None:body["volume"]=str(volume)
        if price is not None:body["price"]=str(price)
        if identifier is not None:body["identifier"]=identifier
        headers={**self._auth(body),"Content-Type":"application/json"}
        r=self.http.post(BASE+"/v1/orders/test",json=body,headers=headers,timeout=5)
        r.raise_for_status();return r.json()

    def submit(self,intent,*,live_allowed=False):
        if not live_allowed:
            return {"status":"BLOCKED","reason":"LIVE_DISABLED","client_order_id":intent.client_order_id}
        raise RuntimeError("LIVE_ORDER_PATH_NOT_IMPLEMENTED")
