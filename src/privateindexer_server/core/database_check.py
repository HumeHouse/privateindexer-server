import asyncio
import datetime
import os

from privateindexer_server.core import mysql, utils
from privateindexer_server.core.config import DATABASE_CHECK_INTERVAL
from privateindexer_server.core.logger import log
from privateindexer_server.core.thread_executor import EXECUTOR


async def check_torrent_database():
    updated_torrents = 0
    removed_torrents = 0

    loop = asyncio.get_running_loop()
    futures = []

    torrents = await mysql.fetch_all("SELECT * FROM torrents")
    for torrent in torrents:
        torrent_path = torrent.get("torrent_path")

        if torrent_path and os.path.exists(torrent_path):
            continue

        future = loop.run_in_executor(EXECUTOR, utils.find_matching_torrent, torrent["hash_v1"], torrent["hash_v2"])
        futures.append(future)

    total_torrents = len(torrents)

    async for future in asyncio.as_completed(futures):
        try:
            matching_torrent, hash_v2 = await future
            if matching_torrent:
                await mysql.execute("UPDATE torrents SET torrent_path = %s WHERE hash_v2 = %s", (matching_torrent, hash_v2))

                updated_torrents += 1
                log.debug(f"[DB-CHECK] Matching torrent found for hash: {hash_v2}")
            else:
                await mysql.execute("DELETE FROM torrents WHERE hash_v2 = %s", (hash_v2,))

                removed_torrents += 1
                log.warning(f"[DB-CHECK] Purged torrent due to no match for hash: {hash_v2}")
        except Exception as e:
            log.error(f"[DB-CHECK] Error in torrent post-hash-check: {e}")

    return total_torrents, updated_torrents, removed_torrents


async def periodic_database_check_task():
    log.debug("[DB-CHECK] Task loop started")
    while True:
        try:
            log.info("[DB-CHECK] Running torrent database check")
            before = datetime.datetime.now()

            total_torrents, updated_torrents, removed_torrents = await check_torrent_database()

            delta = datetime.datetime.now() - before
            log.info(f"[DB-CHECK] Torrent database check complete ({delta}): "
                     f"total {total_torrents} torrents, {updated_torrents} updated, {removed_torrents} removed")
        except Exception as e:
            log.error(f"[DB-CHECK] Error during periodic database check: {e}")
        await asyncio.sleep(DATABASE_CHECK_INTERVAL)
