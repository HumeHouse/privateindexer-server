import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from privateindexer_server.core import mysql, database_check, stale_check, redis, peer_timeout, stats_update, jwt_helper, route_helper
from privateindexer_server.core.config import TORRENTS_DIR, HIGH_LATECY_THRESHOLD, APP_VERSION, DATA_DIR, EXTERNAL_SERVER_URL, REDIS_HOST, \
    MYSQL_HOST, MYSQL_ROOT_PASSWORD, EXTERNAL_TRACKER_URL
from privateindexer_server.core.logger import log
from privateindexer_server.core.routes import gui, admin, torznab, api_v1, api_v2


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info(f"[APP] Starting PrivateIndexer server v{APP_VERSION}")

    # check if data directory exists
    if not os.path.isdir(DATA_DIR):
        log.critical(f"[APP] Data directory does not exist: {DATA_DIR}")
        exit(1)

    # check if data directory has correct permissions
    try:
        test_file = os.path.join(DATA_DIR, ".write_test")
        with open(test_file, "w"):
            pass
        os.unlink(test_file)
    except OSError:
        log.critical(f"[APP] Data directory is not writable: {DATA_DIR}")
        exit(1)

    # try to create torrents directory
    log.info(f"[APP] Torrent data directory: {TORRENTS_DIR}")
    os.makedirs(TORRENTS_DIR, exist_ok=True)

    # ensure server URL set
    if not EXTERNAL_SERVER_URL:
        log.critical(f"[APP] No external server URL set")
        exit(1)

    # TODO: deprecated - remove in upcoming release
    if not EXTERNAL_TRACKER_URL:
        log.critical(f"[APP] No external tracker URL set")
        exit(1)

    # get/create a JWT key used for API
    try:
        jwt_helper.get_jwt_key()
        log.info("[APP] Configured JWT key")
    except Exception as e:
        log.error(f"[APP] Exception while reading/creating JWT key: {e}")
        exit(1)

    # ensure Redis server host is set
    if not REDIS_HOST:
        log.critical(f"[APP] No Redis server host set")
        exit(1)

    # test Redis connection
    try:
        await redis.get_connection()
        log.info("[APP] Connected to Redis")
    except Exception as e:
        log.error(f"[APP] Exception while connecting Redis: {e}")
        exit(1)

    # ensure MySQL host is set
    if not MYSQL_HOST:
        log.critical(f"[APP] No MySQL server host set")
        exit(1)

    # ensure MySQL root password is set
    if not MYSQL_ROOT_PASSWORD:
        log.critical(f"[APP] No MySQL root password set")
        exit(1)

    # test MySQL connection and set up database structure
    try:
        await mysql.setup_database()
        log.info("[APP] Connected to MySQL")
    except Exception as e:
        log.error(f"[APP] Exception while setting up MySQL: {e}")
        exit(1)

    log.info("[APP] Starting periodic tasks")

    # start all periodic server tasks
    app_tasks = [
        asyncio.create_task(stale_check.periodic_stale_check_task()),
        asyncio.create_task(database_check.periodic_database_check_task()),
        asyncio.create_task(peer_timeout.periodic_peer_timeout_task()),
        asyncio.create_task(stats_update.periodic_stats_update_task()),
    ]

    log.info("[APP] API server started on 0.0.0.0:8081")

    yield

    log.info("[APP] Shutting down PrivateIndexer server")

    # stop all periodic tasks
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

app.include_router(gui.router)
app.include_router(admin.router)
app.include_router(torznab.router)
app.include_router(api_v2.router)

# TODO: deprecated - remove in upcoming release
app.include_router(api_v1.router)


@app.middleware("http")
async def track_stats(request: Request, call_next):
    client_ip = route_helper.get_client_ip(request)

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
