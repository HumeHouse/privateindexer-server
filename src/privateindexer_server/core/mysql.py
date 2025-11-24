import asyncio
from typing import Optional

import aiomysql

from privateindexer_server.core.config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_MAX_RETY, MYSQL_RETRY_BACKOFF
from privateindexer_server.core.logger import log

_db_pool: Optional[aiomysql.Pool] = None

USERS_TABLE_SQL = """
                  CREATE TABLE `users`
                  (
                      `id`             int                                     NOT NULL AUTO_INCREMENT,
                      `label`          varchar(100) COLLATE utf8mb4_general_ci NOT NULL,
                      `api_key`        char(64) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                      `downloaded`     bigint unsigned                         NOT NULL DEFAULT '0',
                      `uploaded`       bigint unsigned                         NOT NULL DEFAULT '0',
                      `client_version` text COLLATE utf8mb4_general_ci,
                      `last_ip`        varchar(45) COLLATE utf8mb4_general_ci           DEFAULT NULL,
                      `last_seen`      datetime                                         DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
                      PRIMARY KEY (`id`),
                      UNIQUE KEY `api_key` (`api_key`)
                  ) ENGINE = InnoDB
                    AUTO_INCREMENT = 9
                    DEFAULT CHARSET = utf8mb4
                    COLLATE = utf8mb4_general_ci
                  """

PEERS_TABLE_SQL = """
                  CREATE TABLE `peers`
                  (
                      `id`              bigint unsigned                        NOT NULL AUTO_INCREMENT,
                      `torrent_id`      bigint unsigned                        NOT NULL,
                      `peer_id`         char(40) COLLATE utf8mb4_general_ci    NOT NULL,
                      `ip`              varchar(45) COLLATE utf8mb4_general_ci NOT NULL,
                      `port`            int                                    NOT NULL,
                      `left_bytes`      bigint                                 NOT NULL,
                      `last_seen`       timestamp                              NULL     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      `user_id`         int                                    NOT NULL,
                      `last_downloaded` bigint unsigned                        NOT NULL DEFAULT '0',
                      `last_uploaded`   bigint unsigned                        NOT NULL DEFAULT '0',
                      PRIMARY KEY (`id`),
                      UNIQUE KEY `torrent_id` (`torrent_id`, `peer_id`),
                      KEY `peers_users_id_fk` (`user_id`),
                      CONSTRAINT `peers_torrents_id_fk` FOREIGN KEY (`torrent_id`) REFERENCES `torrents` (`id`) ON DELETE CASCADE,
                      CONSTRAINT `peers_users_id_fk` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
                  ) ENGINE = InnoDB
                    AUTO_INCREMENT = 21586464
                    DEFAULT CHARSET = utf8mb4
                    COLLATE = utf8mb4_general_ci
                  """

TORRENTS_TABLE_SQL = """
                     CREATE TABLE `torrents`
                     (
                         `id`               bigint unsigned                         NOT NULL AUTO_INCREMENT,
                         `name`             varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
                         `normalized_name`  varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
                         `torrent_path`     text COLLATE utf8mb4_general_ci,
                         `size`             bigint                                  NOT NULL,
                         `category`         int                                     NOT NULL,
                         `hash_v1`          char(40) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                         `hash_v2`          char(64) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                         `files`            int                                     NOT NULL,
                         `grabs`            int                                              DEFAULT '0',
                         `added_on`         datetime                                NOT NULL DEFAULT CURRENT_TIMESTAMP,
                         `added_by_user_id` int                                              DEFAULT NULL,
                         `last_seen`        datetime                                         DEFAULT CURRENT_TIMESTAMP,
                         PRIMARY KEY (`id`),
                         UNIQUE KEY `hash_v1` (`hash_v1`),
                         UNIQUE KEY `hash_v2` (`hash_v2`),
                         KEY `torrents_users_id_fk` (`added_by_user_id`),
                         CONSTRAINT `torrents_users_id_fk` FOREIGN KEY (`added_by_user_id`) REFERENCES `users` (`id`) ON DELETE SET NULL
                     ) ENGINE = InnoDB
                       AUTO_INCREMENT = 5392
                       DEFAULT CHARSET = utf8mb4
                       COLLATE = utf8mb4_general_ci
                     """


async def setup_database():
    global _db_pool
    _db_pool = await aiomysql.create_pool(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD, db=MYSQL_DB, autocommit=True)
    log.debug("[MYSQL] Connected to database")

    tables = {"torrents": TORRENTS_TABLE_SQL, "users": USERS_TABLE_SQL, "peers": PEERS_TABLE_SQL, }

    async with _db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            for table_name, create_sql in tables.items():
                await cur.execute("SHOW TABLES LIKE %s", (table_name,))
                exists = await cur.fetchone()
                if not exists:
                    log.info(f"[MYSQL] Creating missing table '{table_name}'")
                    await cur.execute(create_sql)


async def disconnect_database():
    if _db_pool is not None:
        _db_pool.close()
        await _db_pool.wait_closed()
        log.debug("[MYSQL] Connection pool closed")


async def _with_retry(fn, *args, **kwargs):
    for attempt in range(1, MYSQL_MAX_RETY + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            if attempt < MYSQL_MAX_RETY:
                wait_time = MYSQL_RETRY_BACKOFF * attempt
                if attempt > MYSQL_MAX_RETY * .5:
                    log.warning(f"[MYSQL] Query failed with {e}, retrying in {wait_time:.2f}s (attempt {attempt})")
                await asyncio.sleep(wait_time)
                continue
            log.error(f"[MYSQL] Query failed after {MYSQL_MAX_RETY} attempts: {e}")
            raise
    return None


async def fetch_all(query: str, params: tuple = ()):
    async def _do():
        async with _db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()

    return await _with_retry(_do)


async def fetch_one(query: str, params: tuple = ()):
    async def _do():
        async with _db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchone()

    return await _with_retry(_do)


async def execute(query: str, params: tuple = (), include_row_id: bool = False, include_row_count: bool = False):
    async def _do():
        async with _db_pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                result = {}
                if include_row_id:
                    result["lastrowid"] = cur.lastrowid
                if include_row_count:
                    result["rowcount"] = cur.rowcount

                if len(result) == 1:
                    return next(iter(result.values()))

                return result

    return await _with_retry(_do)
