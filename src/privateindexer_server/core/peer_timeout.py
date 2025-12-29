import asyncio
import datetime
import time

from privateindexer_server.core import redis
from privateindexer_server.core.config import PEER_TIMEOUT_INTERVAL, PEER_TIMEOUT
from privateindexer_server.core.logger import log


async def periodic_peer_timeout_task():
    """
    Task to manually expire peers from the Redis database which are older than PEER_TIMEOUT seconds
    """
    log.debug("[PEER-TIMEOUT] Task loop started")
    while True:
        try:
            log.debug("[PEER-TIMEOUT] Running peer timeout check")
            before = datetime.datetime.now()

            redis_connection = redis.get_connection()

            cutoff = int(time.time()) - PEER_TIMEOUT
            total_purged = 0

            # use a cursor loop to scan all peers to prevent Redis database locking
            cursor = 0
            while True:
                # match all peer keys
                cursor, peer_keys = await redis_connection.scan(cursor=cursor, match="peers:*", count=10000, )

                # remove peers which have been living longer than PEER_TIMEOUT seconds
                for peers_key in peer_keys:
                    purged = await redis_connection.zremrangebyscore(peers_key, 0, cutoff, )
                    total_purged += purged

                if cursor == 0:
                    break

            delta = datetime.datetime.now() - before
            log.debug(f"[PEER-TIMEOUT] Completed in {delta}, purged {total_purged} peers")
        except Exception as e:
            log.error(f"[PEER-TIMEOUT] Error during periodic peer timeout check: {e}")
        await asyncio.sleep(PEER_TIMEOUT_INTERVAL)
