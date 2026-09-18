import json
import tempfile
import unittest
from pathlib import Path
from aiohttp.test_utils import TestClient, TestServer
from magi1.intelligence_http import make_app

class HttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.client=TestClient(TestServer(make_app(self.root,'t'*32)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_auth_and_missing_snapshot(self):
        response=await self.client.get('/intelligence')
        self.assertEqual(response.status,401)
        response=await self.client.get('/intelligence',headers={'Authorization':'Bearer '+'t'*32})
        self.assertEqual(response.status,503)

    async def test_serves_only_snapshot_no_write_route(self):
        path=self.root/'exports'/'intelligence_latest.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'observations':[]}))
        headers={'Authorization':'Bearer '+'t'*32}
        response=await self.client.get('/intelligence',headers=headers)
        self.assertEqual(await response.json(),{'observations':[]})
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        response=await self.client.post('/intelligence',headers=headers)
        self.assertEqual(response.status,405)
        response=await self.client.get('/research.db',headers=headers)
        self.assertEqual(response.status,404)

    def test_short_token_rejected(self):
        with self.assertRaises(ValueError): make_app(self.root,'1')
