import asyncio
import datetime

from privateindexer_server.core import mysql
from privateindexer_server.core.config import STATS_UPDATE_INTERVAL, PEER_TIMEOUT
from privateindexer_server.core.logger import log


async def periodic_stats_update_task():
    log.debug("[STATS-UPDATE] Task loop started")
    while True:
        try:
            log.debug("[STATS-UPDATE] Running stats update")
            before = datetime.datetime.now()

            stats_query = """
                          UPDATE users u
                              LEFT JOIN (SELECT added_by_user_id        AS user_id,
                                                COUNT(*)                AS torrents_uploaded,
                                                COALESCE(SUM(grabs), 0) AS grabs
                                         FROM torrents
                                         GROUP BY added_by_user_id) t ON u.id = t.user_id
                              LEFT JOIN (SELECT user_id,
                                                SUM(left_bytes = 0) AS seeding,
                                                SUM(left_bytes > 0) AS leeching
                                         FROM peers
                                         WHERE last_seen > NOW() - INTERVAL %s SECOND
                                         GROUP BY user_id) p ON u.id = p.user_id
                          SET u.torrents_uploaded = COALESCE(t.torrents_uploaded, 0),
                              u.grabs             = COALESCE(t.grabs, 0),
                              u.seeding           = COALESCE(p.seeding, 0),
                              u.leeching          = COALESCE(p.leeching, 0)
                          WHERE TRUE;
                          """

            await mysql.execute(stats_query, (PEER_TIMEOUT,))

            delta = datetime.datetime.now() - before
            log.debug(f"[STATS-UPDATE] Stats update complete ({delta})")
        except Exception as e:
            log.error(f"[STATS-UPDATE] Error during periodic stats update: {e}")
        await asyncio.sleep(STATS_UPDATE_INTERVAL)
