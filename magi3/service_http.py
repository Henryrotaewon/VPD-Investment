"""Private authenticated JSON snapshots; no mutation routes."""
import hmac
from aiohttp import web
from .runtime_store import encode


def make_app(token,readers):
    if len(token)<32:raise ValueError('SERVICE_TOKEN_TOO_SHORT')
    async def get(request):
        if not hmac.compare_digest(request.headers.get('Authorization','').encode(),('Bearer '+token).encode()):
            raise web.HTTPUnauthorized()
        try:
            result=readers[request.path](request) if request.path=='/orders/recent' else readers[request.path]()
        except (ValueError,OverflowError):raise web.HTTPBadRequest(text='INVALID_WINDOW')
        if result is None:raise web.HTTPServiceUnavailable(text='SNAPSHOT_NOT_READY')
        return web.Response(text=encode(result),content_type='application/json',headers={'Cache-Control':'no-store'})
    app=web.Application()
    for path in readers:app.router.add_get(path,get)
    return app
