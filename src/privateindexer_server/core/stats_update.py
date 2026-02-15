import asyncio
import datetime
from collections import defaultdict

from privateindexer_server.core import mysql, redis
from privateindexer_server.core.config import STATS_UPDATE_INTERVAL
from privateindexer_server.core import logger


async def periodic_stats_update_task():
    """
    Task to update internal tracking statistics for each user based on Redis data
    :return:
    """
    logger.channel("stats-update").debug("Task loop started")
    while True:
        try:
            logger.channel("stats-update").debug("Running stats update")
            before = datetime.datetime.now()
            redis_connection = redis.get_connection()

            # create a base dict for tracking stats per user
            all_user_stats = defaultdict(lambda: {"seeding": 0, "leeching": 0})

            # use a cursor to fetch all peer data to prevent Redis database locking
            cursor = 0
            while True:
                cursor, peer_keys = await redis_connection.scan(cursor=cursor, match="peer:*:*", count=10000)
                for peer_key in peer_keys:
                    # fetch the peer mapping data for this peer ID
                    peer_data = await redis_connection.hgetall(peer_key)
                    if not peer_data:
                        continue

                    # skip invalid peer data
                    try:
                        user_id = int(peer_data["user_id"])
                        left = int(peer_data["left"])
                    except (KeyError, ValueError):
                        continue

                    # increment seeds/leeches based on number of data peices needed by peer
                    if left == 0:
                        all_user_stats[user_id]["seeding"] += 1
                    else:
                        all_user_stats[user_id]["leeching"] += 1

                if cursor == 0:
                    break

            # update each user we have peer data for
            for user_id, user_stats in all_user_stats.items():
                seeding = user_stats["seeding"]
                leeching = user_stats["leeching"]
                await mysql.execute("UPDATE users SET seeding=%s, leeching=%s WHERE id=%s", (seeding, leeching, user_id,))

            # update all user stats for torrents tracked in database
            await mysql.execute("""
                                UPDATE users u
                                    LEFT JOIN (SELECT added_by_user_id        AS user_id,
                                                      COUNT(*)                AS torrents_uploaded,
                                                      COALESCE(SUM(grabs), 0) AS grabs
                                               FROM torrents
                                               GROUP BY added_by_user_id) t ON u.id = t.user_id
                                SET u.torrents_uploaded = COALESCE(t.torrents_uploaded, 0),
                                    u.popularity        = COALESCE(t.grabs, 0)
                                WHERE TRUE
                                """)

            delta = datetime.datetime.now() - before
            logger.channel("stats-update").debug(f"Stats update complete ({delta})")
        except Exception as e:
            logger.channel("stats-update").error(f"Error during periodic stats update: {e}")
        await asyncio.sleep(STATS_UPDATE_INTERVAL)
