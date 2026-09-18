import unittest
from aiohttp.test_utils import TestClient,TestServer
from magi3.service_http import make_app

class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_no_mutation_and_not_ready(self):
        client=TestClient(TestServer(make_app('s'*32,{'/shadow':lambda:{'mode':'SHADOW'},'/accounts':lambda:None})))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/shadow')).status,401)
            h={'Authorization':'Bearer '+'s'*32}
            self.assertEqual((await client.get('/shadow',headers=h)).status,200)
            self.assertEqual((await client.get('/accounts',headers=h)).status,503)
            self.assertEqual((await client.post('/shadow',headers=h)).status,405)
        finally:await client.close()

    async def test_recent_orders_query_validation_and_read_only(self):
        import tempfile
        from magi3.runtime_store import RuntimeStore
        with tempfile.TemporaryDirectory() as root:
            store=RuntimeStore(root)
            client=TestClient(TestServer(make_app('s'*32,{'/orders/recent':lambda request:store.orders_window(request.query.get('until'),request.query.get('offset',0))})))
            await client.start_server()
            try:
                headers={'Authorization':'Bearer '+'s'*32}
                self.assertEqual((await client.get('/orders/recent')).status,401)
                response=await client.get('/orders/recent?offset=0',headers=headers)
                self.assertEqual(response.status,200)
                report=await response.json()
                self.assertEqual(report['until_ts_ms']-report['since_ts_ms'],72*3600000)
                self.assertEqual(report['total'],0)
                for query in ('offset=-1','until=invalid','offset=invalid'):
                    self.assertEqual((await client.get('/orders/recent?'+query,headers=headers)).status,400)
                self.assertEqual((await client.post('/orders/recent',headers=headers)).status,405)
            finally:
                await client.close();store.close()
