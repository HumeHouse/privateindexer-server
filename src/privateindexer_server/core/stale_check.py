import asyncio
import datetime

from privateindexer_server.core import mysql
from privateindexer_server.core.config import STALE_CHECK_INTERVAL, STALE_THRESHOLD
from privateindexer_server.core.logger import log


async def periodic_stale_check_task():
    """
    Task to purge stale/inactive torrents from database
    """
    log.debug("[STALE-CHECK] Task loop started")
    while True:
        try:
            log.info("[STALE-CHECK] Running stale torrents check")
            before = datetime.datetime.now()

            # purge torrents which have not been seen in at least STALE_THRESHOLD number of seconds
            removed_torrents = await mysql.execute("DELETE FROM torrents WHERE last_seen < NOW() - INTERVAL %s SECOND", (STALE_THRESHOLD,), include_row_count=True)

            delta = datetime.datetime.now() - before
            log.info(f"[STALE-CHECK] Stale torrents check complete ({delta}): purged {removed_torrents} torrents")
        except Exception as e:
            log.error(f"[STALE-CHECK] Error during periodic database check: {e}")
        await asyncio.sleep(STALE_CHECK_INTERVAL)
