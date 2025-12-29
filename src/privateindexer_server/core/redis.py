import redis.asyncio as redis

from privateindexer_server.core.config import REDIS_HOST
from privateindexer_server.core.logger import log

_redis_connection: redis.Redis | None = None


def get_connection() -> redis.Redis:
    global _redis_connection

    if _redis_connection is None:
        _redis_connection = redis.Redis(host=REDIS_HOST, decode_responses=True, )
        log.debug("[REDIS] Redis client initizalized")

    return _redis_connection


async def close_connection():
    global _redis_connection
    if _redis_connection is not None:
        await _redis_connection.close()
