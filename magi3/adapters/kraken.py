"""Kraken read-only account and public market client (no order endpoints)."""
import base64
import hashlib
import hmac
import os
import threading
import time
from urllib.parse import urlencode
import requests
from .base import require_credentials
BASE='https://api.kraken.com'


def sign(path,data,secret):
    encoded=(str(data['nonce'])+urlencode(data)).encode()
    digest=hashlib.sha256(encoded).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret),path.encode()+digest,hashlib.sha512).digest()).decode()


class KrakenAdapter:
    venue='kraken'
    def __init__(self,session=None,nonce_provider=None):
        self.http=session or requests.Session()
        self.nonce_provider=nonce_provider
        self._nonce=0
        self._lock=threading.Lock()

    def public(self,path,params=None):
        r=self.http.get(BASE+'/0/public/'+path,params=params,timeout=8)
        r.raise_for_status();data=r.json()
        if data.get('error'): raise RuntimeError('KRAKEN_PUBLIC_REJECTED')
        return data

    def orderbook(self,pair,count=100):return self.public('Depth',{'pair':pair,'count':count})
    def assets(self):return self.public('Assets')['result']
    def pairs(self):return self.public('AssetPairs')['result']
    def ticker(self,pair):return self.public('Ticker',{'pair':pair})['result']

    def balance(self):
        require_credentials('KRAKEN_API_KEY','KRAKEN_PRIVATE_KEY')
        with self._lock:
            self._nonce=(self.nonce_provider() if self.nonce_provider else max(self._nonce+1,time.time_ns()//1000000))
            data={'nonce':str(self._nonce)}
            path='/0/private/BalanceEx'
            headers={'API-Key':os.environ['KRAKEN_API_KEY'],
                     'API-Sign':sign(path,data,os.environ['KRAKEN_PRIVATE_KEY'])}
            r=self.http.post(BASE+path,data=data,headers=headers,timeout=8)
            r.raise_for_status();payload=r.json()
            if payload.get('error'):raise RuntimeError('KRAKEN_ACCOUNT_REJECTED')
            return payload['result']
