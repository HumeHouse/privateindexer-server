import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from privateindexer_server.core import mysql, api, database_check, stale_check, redis, utils, peer_timeout, stats_update
from privateindexer_server.core.config import TORRENTS_DIR, HIGH_LATECY_THRESHOLD, APP_VERSION
from privateindexer_server.core.logger import log


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(f"[APP] Starting PrivateIndexer server v{APP_VERSION}")

    if not os.path.exists(TORRENTS_DIR):
        log.info(f"[APP] Creating torrents directory: {TORRENTS_DIR}")
        os.makedirs(TORRENTS_DIR)

    log.info("[APP] Connecting Redis")

    redis.connect_database()

    log.info("[APP] Connecting and setting up MySQL")

    await mysql.setup_database()

    log.info("[APP] Starting periodic tasks")

    app_tasks = [
        asyncio.create_task(database_check.periodic_database_check_task()),
        asyncio.create_task(stale_check.periodic_stale_check_task()),
        asyncio.create_task(peer_timeout.periodic_peer_timeout_task()),
        asyncio.create_task(stats_update.periodic_stats_update_task()),
    ]

    log.info("[APP] API server started on 0.0.0.0:80")

    yield

    log.info("[APP] Shutting down PrivateIndexer server")

    for task in app_tasks:
        try:
            task.cancel()
        except Exception:
            pass

    await mysql.disconnect_database()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
              title=f"PrivateIndexer Server", version=APP_VERSION)

app.include_router(api.router)


@app.middleware("http")
async def track_stats(request: Request, call_next):
    redis_connection = redis.get_connection()
    client_ip = utils.get_client_ip(request)

    pipe = redis_connection.pipeline()
    _ = pipe.incr("stats:requests")
    _ = pipe.sadd("stats:unique_ips", client_ip)

    if request.headers.get("content-length"):
        _ = pipe.incrby("stats:bytes_received", int(request.headers["content-length"]))

    start_time = time.perf_counter()
    response: Response = await call_next(request)
    duration = (time.perf_counter() - start_time) * 1000

    request_method = request.scope.get("method")
    request_string = request.scope.get("path")
    query_string = request.scope.get("query_string")
    if query_string:
        request_string = f"{request_string}?{query_string.decode()}"

    if duration > HIGH_LATECY_THRESHOLD:
        log.warning(f"[APP] High response time ({duration} ms) - [{request_method}] {request_string}")
    else:
        log.debug(f"[APP] Request ({duration} ms) - [{request_method}] {request_string}")

    if response.headers.get("content-length"):
        _ = pipe.incrby("stats:bytes_sent", int(response.headers["content-length"]))

    pipe.execute()

    return response
