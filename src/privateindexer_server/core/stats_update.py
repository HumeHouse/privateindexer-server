import asyncio
import datetime
from collections import defaultdict

from privateindexer_server.core import mysql, redis
from privateindexer_server.core.config import STATS_UPDATE_INTERVAL
from privateindexer_server.core.logger import log


async def periodic_stats_update_task():
    log.debug("[STATS-UPDATE] Task loop started")
    while True:
        try:
            log.debug("[STATS-UPDATE] Running stats update")
            before = datetime.datetime.now()
            redis_connection = redis.get_connection()

            all_user_stats = defaultdict(lambda: {"seeding": 0, "leeching": 0})

            cursor = 0
            while True:
                cursor, keys = await redis_connection.scan(cursor=cursor, match="peer:*:*", count=10000)
                for key in keys:
                    pdata = await redis_connection.hgetall(key)
                    if not pdata:
                        continue

                    try:
                        user_id = int(pdata["user_id"])
                        left = int(pdata["left"])
                    except (KeyError, ValueError):
                        continue

                    if left == 0:
                        all_user_stats[user_id]["seeding"] += 1
                    else:
                        all_user_stats[user_id]["leeching"] += 1

                if cursor == 0:
                    break

            for user_id, user_stats in all_user_stats.items():
                seeding = user_stats["seeding"]
                leeching = user_stats["leeching"]
                await mysql.execute("UPDATE users SET seeding=%s, leeching=%s WHERE id=%s", (seeding, leeching, user_id,))

            await mysql.execute("""
                                UPDATE users u
                                    LEFT JOIN (SELECT added_by_user_id        AS user_id,
                                                      COUNT(*)                AS torrents_uploaded,
                                                      COALESCE(SUM(grabs), 0) AS grabs
                                               FROM torrents
                                               GROUP BY added_by_user_id) t ON u.id = t.user_id
                                SET u.torrents_uploaded = COALESCE(t.torrents_uploaded, 0),
                                    u.grabs             = COALESCE(t.grabs, 0)
                                WHERE TRUE
                                """)

            delta = datetime.datetime.now() - before
            log.debug(f"[STATS-UPDATE] Stats update complete ({delta})")
        except Exception as e:
            log.error(f"[STATS-UPDATE] Error during periodic stats update: {e}")
        await asyncio.sleep(STATS_UPDATE_INTERVAL)
