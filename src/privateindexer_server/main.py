import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from privateindexer_server.core import mysql, api, database_check, stale_check, redis, utils, peer_timeout, stats_update, gui
from privateindexer_server.core.config import TORRENTS_DIR, HIGH_LATECY_THRESHOLD, APP_VERSION
from privateindexer_server.core.logger import log


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(f"[APP] Starting PrivateIndexer server v{APP_VERSION}")

    # ensure torrents directory exists
    if not os.path.exists(TORRENTS_DIR):
        log.info(f"[APP] Creating torrents directory: {TORRENTS_DIR}")
        os.makedirs(TORRENTS_DIR)

    log.info("[APP] Connecting Redis")

    await redis.get_connection()

    log.info("[APP] Connecting and setting up MySQL")

    await mysql.setup_database()

    log.info("[APP] Starting periodic tasks")

    # start all periodic server tasks
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

    await redis.close_connection()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
              title=f"PrivateIndexer Server", version=APP_VERSION)

app.mount("/static", StaticFiles(directory="/app/src/static"), name="static")

app.include_router(api.router)
app.include_router(gui.router)


@app.middleware("http")
async def track_stats(request: Request, call_next):
    client_ip = utils.get_client_ip(request)

    # start a redis transaction
    redis_connection = redis.get_connection()
    pipe = redis_connection.pipeline()

    # append client IP to known IP list and increment request counter
    await pipe.incr("stats:requests")
    await pipe.sadd("stats:unique_ips", client_ip)

    # add the requester-side content length to the counter
    if request.headers.get("content-length"):
        await pipe.incrby("stats:bytes_received", int(request.headers["content-length"]))

    # time the endpoint execution
    start_time = time.perf_counter()
    response: Response = await call_next(request)
    duration = (time.perf_counter() - start_time) * 1000

    # parse the request parts and the query parameters
    request_method = request.scope.get("method")
    request_string = request.scope.get("path")
    query_string = request.scope.get("query_string")
    if query_string:
        request_string = f"{request_string}?{query_string.decode()}"

    # see if this request endpoint has a custom threshold, otherwise use the default
    threshold = getattr(request.state, "latency_threshold", HIGH_LATECY_THRESHOLD)

    # check the endpoint execution time for high latency
    if duration > threshold:
        log.warning(f"[APP] High response time ({duration} ms) - [{request_method}] {request_string}")
    else:
        log.debug(f"[APP] Request ({duration} ms) - [{request_method}] {request_string}")

    # add the server-side content length to the counter
    if response.headers.get("content-length"):
        await pipe.incrby("stats:bytes_sent", int(response.headers["content-length"]))

    # complete the redis transation
    await pipe.execute()

    return response
