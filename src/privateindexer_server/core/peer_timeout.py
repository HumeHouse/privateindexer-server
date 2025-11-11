import asyncio
import datetime

from privateindexer_server.core import mysql
from privateindexer_server.core.config import PEER_TIMEOUT_INTERVAL, PEER_TIMEOUT
from privateindexer_server.core.logger import log


async def periodic_peer_timeout_task():
    log.debug("[PEER-TIMEOUT] Task loop started")
    while True:
        try:
            log.debug("[PEER-TIMEOUT] Running peer timeout check")
            before = datetime.datetime.now()

            purged_peers = await mysql.execute("DELETE FROM peers WHERE last_seen < NOW() - INTERVAL %s SECOND", (PEER_TIMEOUT,))

            delta = datetime.datetime.now() - before
            log.debug(f"[PEER-TIMEOUT] Peer timeout check complete ({delta})")
            purged_count = len(purged_peers)
            if purged_count:
                log.info(f"[PEER-TIMEOUT] Peer timeout check purged {purged_count} peers")
        except Exception as e:
            log.error(f"[PEER-TIMEOUT] Error during periodic peer timeout check: {e}")
        await asyncio.sleep(PEER_TIMEOUT_INTERVAL)
