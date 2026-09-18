import os
import time
import uuid
import jwt
import requests
from .base import require_credentials
BASE='https://api.bithumb.com'

class BithumbAdapter:
    venue='bithumb'
    def __init__(self,session=None):self.http=session or requests.Session()
    def orderbook(self,market):
        r=self.http.get(BASE+'/v1/orderbook',params={'markets':market},timeout=8)
        r.raise_for_status();return r.json()
    def accounts(self):
        require_credentials('BITHUMB_ACCESS_KEY','BITHUMB_SECRET_KEY')
        payload={'access_key':os.environ['BITHUMB_ACCESS_KEY'],'nonce':str(uuid.uuid4()),
                 'timestamp':time.time_ns()//1000000}
        token=jwt.encode(payload,os.environ['BITHUMB_SECRET_KEY'],algorithm='HS256')
        r=self.http.get(BASE+'/v1/accounts',headers={'Authorization':'Bearer '+token},timeout=8)
        r.raise_for_status();return r.json()
