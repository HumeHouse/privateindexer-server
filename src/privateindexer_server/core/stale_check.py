import asyncio
import datetime
import os

from privateindexer_server.core import mysql, utils
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

            # fetch torrents which have not been seen in at least STALE_THRESHOLD number of seconds
            stale_torrents = await mysql.fetch_all("SELECT id, hash_v2 FROM torrents WHERE last_seen < NOW() - INTERVAL %s SECOND", (STALE_THRESHOLD,))

            removed_torrents = 0

            # loop through each stale torrent to remove the file if it exists
            for stale_torrent in stale_torrents:
                torrent_file = utils.get_torrent_file(stale_torrent["hash_v2"])

                # remove the file
                if os.path.exists(torrent_file):
                    os.unlink(torrent_file)

                # remove from database
                await mysql.execute("DELETE FROM torrents WHERE id = %s", (stale_torrent["id"],))
                removed_torrents += 1

            delta = datetime.datetime.now() - before
            log.info(f"[STALE-CHECK] Stale torrents check complete ({delta}): purged {removed_torrents} torrents")
        except Exception as e:
            log.error(f"[STALE-CHECK] Error during periodic database check: {e}")
        await asyncio.sleep(STALE_CHECK_INTERVAL)
