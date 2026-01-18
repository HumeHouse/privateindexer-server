import asyncio
import datetime
import os

from privateindexer_server.core import mysql, utils
from privateindexer_server.core.config import DATABASE_CHECK_INTERVAL, TORRENTS_DIR
from privateindexer_server.core.logger import log


async def check_torrent_database():
    """
    Checks the torrent database torrent file paths for existence or tries to locate a matching file on disk
    """
    removed_torrents = 0

    # fetch all the torrents
    torrents = await mysql.fetch_all("SELECT id, hash_v2 FROM torrents")
    all_v2_hashes = {torrent["hash_v2"] for torrent in torrents}
    total_torrents = len(torrents)

    # loop through each torrent
    for torrent in torrents:
        torrent_file = utils.get_torrent_file(torrent["hash_v2"])

        # skip if the torrent file already exists
        if os.path.exists(torrent_file):
            continue

        # purge the torrent if the file doesn't exist
        await mysql.execute("DELETE FROM torrents WHERE id = %s", (torrent["id"],))

        removed_torrents += 1
        log.warning(f"[DB-CHECK] Purged torrent due to missing torrent file for hash: {torrent["hash_v2"]}")

    # loop through all the files in the torrents directory
    for filename in os.listdir(TORRENTS_DIR):
        hash_v2 = os.path.splitext(filename)[0]

        # check the name of the file against the database - should match v2 hash
        if hash_v2 not in all_v2_hashes:
            log.warning(f"[DB-CHECK] Purged torrent file due to not tracked by database: {filename}")
            os.unlink(os.path.join(TORRENTS_DIR, filename))

    return total_torrents, removed_torrents


async def periodic_database_check_task():
    """
    Task to check the torrent database against files on the disk
    """
    log.debug("[DB-CHECK] Task loop started")
    while True:
        try:
            log.info("[DB-CHECK] Running torrent database check")
            before = datetime.datetime.now()

            total_torrents, removed_torrents = await check_torrent_database()

            delta = datetime.datetime.now() - before
            log.info(f"[DB-CHECK] Torrent database check complete ({delta}): {total_torrents} torrents, {removed_torrents} removed")
        except Exception as e:
            log.error(f"[DB-CHECK] Error during periodic database check: {e}")
        await asyncio.sleep(DATABASE_CHECK_INTERVAL)
