import redis.asyncio as redis

from privateindexer_server.core import logger
from privateindexer_server.core.config import REDIS_HOST

_redis_connection: redis.Redis | None = None


def get_connection() -> redis.Redis:
    """
    Creates and returns a Redis connection
    """
    global _redis_connection

    if _redis_connection is None:
        _redis_connection = redis.Redis(host=REDIS_HOST, decode_responses=True, )
        logger.channel("redis").debug("Redis client initizalized")

    return _redis_connection


async def close_connection():
    """
    Destroys Redis connection if active
    """
    global _redis_connection
    if _redis_connection is not None:
        await _redis_connection.close()
