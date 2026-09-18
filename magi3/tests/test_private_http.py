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
