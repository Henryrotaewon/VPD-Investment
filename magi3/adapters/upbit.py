"""Phase-0 Upbit adapter. Public market data only; no private/order path."""
import requests
BASE="https://api.upbit.com"
class UpbitAdapter:
    venue="upbit"
    def orderbook(self,market:str):
        r=requests.get(BASE+"/v1/orderbook",params={"markets":market},timeout=5);r.raise_for_status();return r.json()
    def submit(self,intent,*,live_allowed=False):
        if not live_allowed:return {"status":"BLOCKED","reason":"LIVE_DISABLED","client_order_id":intent.client_order_id}
        raise RuntimeError("LIVE_ORDER_PATH_NOT_IMPLEMENTED")
