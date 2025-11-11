from redis import Redis

from privateindexer_server.core.config import REDIS_HOST
from privateindexer_server.core.logger import log

_redis_connection: Redis = None


def connect_database() -> Redis:
    global _redis_connection
    _redis_connection = Redis(host=REDIS_HOST)
    log.debug("[REDIS] Connected to database")
    return _redis_connection


def get_connection() -> Redis:
    global _redis_connection
    if not _redis_connection:
        return connect_database()
    return _redis_connection
