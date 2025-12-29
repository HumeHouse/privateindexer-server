import asyncio
import datetime
import os

from privateindexer_server.core import mysql, utils, thread_executor
from privateindexer_server.core.config import DATABASE_CHECK_INTERVAL
from privateindexer_server.core.logger import log


async def check_torrent_database():
    """
    Checks the torrent database torrent file paths for existence or tries to locate a matching file on disk
    """
    updated_torrents = 0
    removed_torrents = 0

    loop = asyncio.get_running_loop()
    futures = []

    # open a hash executor for calculating torrent hashes
    hash_executor = thread_executor.get_hash_executor()

    # fetch all the torrents and loop through each
    torrents = await mysql.fetch_all("SELECT * FROM torrents")
    for torrent in torrents:
        torrent_path = torrent.get("torrent_path")

        # skip the torrent if the stored file path already exists
        if torrent_path and os.path.exists(torrent_path):
            continue

        # try to find a matching torrent if the file we're tracking is on disk
        future = loop.run_in_executor(hash_executor, utils.find_matching_torrent, torrent["hash_v1"], torrent["hash_v2"])
        futures.append(future)

    total_torrents = len(torrents)

    async for future in asyncio.as_completed(futures):
        try:
            matching_torrent, hash_v2 = await future
            # if we found a match, update the database with the new torrent path
            if matching_torrent:
                await mysql.execute("UPDATE torrents SET torrent_path = %s WHERE hash_v2 = %s", (matching_torrent, hash_v2))

                updated_torrents += 1
                log.debug(f"[DB-CHECK] Matching torrent found for hash: {hash_v2}")

            # if no matching file was found, purge the torrent entry from the database
            else:
                await mysql.execute("DELETE FROM torrents WHERE hash_v2 = %s", (hash_v2,))

                removed_torrents += 1
                log.warning(f"[DB-CHECK] Purged torrent due to no match for hash: {hash_v2}")
        except Exception as e:
            log.error(f"[DB-CHECK] Error in torrent post-hash-check: {e}")

    # close the hash executor process pool
    hash_executor.shutdown()
    log.debug(f"[DB-CHECK] Hash executor workers closed")

    return total_torrents, updated_torrents, removed_torrents


async def periodic_database_check_task():
    """
    Task to check the torrent database against files on the disk
    """
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
