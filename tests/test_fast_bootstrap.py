import gzip
import json
import tempfile
from pathlib import Path
import unittest
from magi1.fast_observe import Observer,DAY,FIVE
from magi1.fast_bootstrap import initialize,import_archive,fill,prune,bounds

class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.cutoff=31*DAY
        self.o=Observer(str(Path(self.tmp.name)/'data.db'),self.cutoff);self.db=self.o.db;initialize(self.db)
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def test_archive_reuse_prune_and_future_excluded(self):
        path=Path(self.tmp.name)/'KRW-X_5m.json.gz'
        rows=[[self.cutoff-2*DAY,1,2,1,2,5,8],[self.cutoff-FIVE,1,2,1,2,5,8],[self.cutoff,1,2,1,2,5,8]]
        path.write_bytes(gzip.compress(json.dumps(rows).encode()))
        self.assertEqual(import_archive(self.db,path,self.cutoff)['usable_candles'],2)
        import_archive(self.db,path,self.cutoff)
        self.assertEqual(self.db.execute('SELECT count(*) FROM baseline').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM recent5m').fetchone()[0],1)
        prune(self.db,self.cutoff+DAY)
        self.assertEqual(self.db.execute('SELECT count(*) FROM recent5m').fetchone()[0],0)
    def test_missing_only_and_zero_proof(self):
        start,end=bounds(self.cutoff)
        with self.db:
            for t in range(start,end,FIVE):
                if t==end-FIVE:continue
                self.db.execute('INSERT INTO baseline VALUES(?,?,1)',('KRW-X',t))
                if t>=end-DAY:self.db.execute('INSERT INTO recent5m VALUES(?,?,1,1,1,1,1,1,?)',('KRW-X',t,'ARCHIVE'))
        class Client:
            def __init__(self):self.calls=0
            def candles(_,symbol,cursor):
                from datetime import datetime,timezone
                _.calls+=1
                return [dict(candle_date_time_utc=datetime.fromtimestamp((end-2*FIVE)/1000,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),opening_price=1,high_price=1,low_price=1,trade_price=1,candle_acc_trade_volume=1,candle_acc_trade_price=1)]
        client=Client();r=fill(self.db,'KRW-X',end,client)
        self.assertEqual(r['missing_buckets'],0);self.assertEqual(client.calls,1)
        self.assertEqual(self.db.execute('SELECT o,value,source FROM recent5m WHERE bucket=?',(end-FIVE,)).fetchone(),(None,0.,'API_NO_TRADE'))
        fill(self.db,'KRW-X',end,client);self.assertEqual(client.calls,1)
    def test_empty_api_never_invents_history(self):
        class Client:
            def candles(self,*args):return []
        result=fill(self.db,'KRW-NEW',self.cutoff,Client())
        self.assertGreater(result['missing_buckets'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM baseline').fetchone()[0],0)

if __name__=='__main__':unittest.main()
