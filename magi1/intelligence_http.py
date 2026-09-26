"""Opt-in authenticated read-only endpoint on Railway's private network."""
import asyncio
import hmac
import json
import os
from pathlib import Path
from aiohttp import web


def make_app(root, token):
    if len(token) < 32:
        raise ValueError('INTELLIGENCE_TOKEN_TOO_SHORT')
    paths={'/intelligence': Path(root)/'exports'/'intelligence_latest.json',
           '/wave': Path(root)/'exports'/'wave_latest.json',
           '/market-context': Path(root)/'exports'/'market_context_latest.json'}

    async def get_snapshot(request):
        supplied=request.headers.get('Authorization','')
        if not hmac.compare_digest(supplied.encode(),('Bearer '+token).encode()):
            raise web.HTTPUnauthorized()
        if request.path=='/wave':
            raise web.HTTPGone(text='WAVE_RETIRED_USE_MAGI2_INDICATOR_PAPER')
        try:
            payload=await asyncio.to_thread(paths[request.path].read_text,encoding='utf-8')
            json.loads(payload)
        except (OSError,ValueError):
            raise web.HTTPServiceUnavailable(text='SNAPSHOT_NOT_READY')
        return web.Response(text=payload,content_type='application/json',
                            headers={'Cache-Control':'no-store'})

    app=web.Application()
    app.router.add_get('/intelligence',get_snapshot)
    app.router.add_get('/wave',get_snapshot)
    app.router.add_get('/market-context',get_snapshot)
    return app


async def serve(root,log):
    runner=None
    try:
        app=make_app(root,os.getenv('MAGI_INTELLIGENCE_TOKEN',''))
        runner=web.AppRunner(app,access_log=None)
        await runner.setup()
        await web.TCPSite(runner,host=os.getenv('MAGI1_INTELLIGENCE_BIND','::'),
                          port=int(os.getenv('MAGI1_INTELLIGENCE_PORT','8081'))).start()
        log.info('intelligence_http_started read_only=true')
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A transport failure must not interrupt existing market collection.
        log.error('intelligence_http_failed type=%s',type(exc).__name__)
    finally:
        if runner is not None: await runner.cleanup()
