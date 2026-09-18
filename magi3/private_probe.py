"""Manual phase-1 connectivity probe. No live order endpoint is called."""
import argparse,json
from .adapters.upbit import UpbitAdapter

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--market",default="KRW-BTC")
    p.add_argument("--order-test-krw",type=int,default=None)
    a=p.parse_args();u=UpbitAdapter()
    out={"accounts":u.accounts(),"chance":u.order_chance(a.market)}
    if a.order_test_krw:
        out["order_test"]=u.order_test(a.market,"bid","price",price=a.order_test_krw)
    print(json.dumps(out,ensure_ascii=False))

if __name__=="__main__":main()
