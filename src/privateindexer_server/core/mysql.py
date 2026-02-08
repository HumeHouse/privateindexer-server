import asyncio
import warnings
from typing import Optional

import aiomysql

from privateindexer_server.core.config import MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB, MYSQL_MAX_RETY, MYSQL_RETRY_BACKOFF, MYSQL_ROOT_PASSWORD
from privateindexer_server.core.logger import log

_db_pool: Optional[aiomysql.Pool] = None

USERS_TABLE_SQL = """
                  CREATE TABLE `users`
                  (
                      `id`                int                                     NOT NULL AUTO_INCREMENT,
                      `label`             varchar(100) COLLATE utf8mb4_general_ci NOT NULL,
                      `api_key`           char(64) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                      `downloaded`        bigint unsigned                         NOT NULL DEFAULT 0,
                      `uploaded`          bigint unsigned                         NOT NULL DEFAULT 0,
                      `torrents_uploaded` int unsigned                            NOT NULL DEFAULT 0,
                      `popularity`        int unsigned                            NOT NULL DEFAULT 0,
                      `grabs`             int unsigned                            NOT NULL DEFAULT 0,
                      `seeding`           int unsigned                            NOT NULL DEFAULT 0,
                      `leeching`          int unsigned                            NOT NULL DEFAULT 0,
                      `client_version`    text COLLATE utf8mb4_general_ci,
                      `last_ip`           varchar(45) COLLATE utf8mb4_general_ci           DEFAULT NULL,
                      `last_seen`         datetime                                         DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
                      `reachable`         tinyint                                 NOT NULL DEFAULT 0,
                      `public_uploads`    tinyint                                 NOT NULL DEFAULT 0,
                      PRIMARY KEY (`id`),
                      UNIQUE KEY `api_key` (`api_key`)
                  ) ENGINE = InnoDB
                    AUTO_INCREMENT = 9
                    DEFAULT CHARSET = utf8mb4
                    COLLATE = utf8mb4_general_ci
                  """

TORRENTS_TABLE_SQL = """
                     CREATE TABLE `torrents`
                     (
                         `id`               bigint unsigned                         NOT NULL AUTO_INCREMENT,
                         `name`             varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
                         `normalized_name`  varchar(255) COLLATE utf8mb4_general_ci NOT NULL,
                         `season`           smallint                                NULL,
                         `episode`          smallint                                NULL,
                         `imdbid`           int                                     NULL,
                         `tmdbid`           int                                     NULL,
                         `tvdbid`           int                                     NULL,
                         `artist`           text COLLATE utf8mb4_general_ci         NULL,
                         `album`            text COLLATE utf8mb4_general_ci         NULL,
                         `size`             bigint                                  NOT NULL,
                         `category`         int                                     NOT NULL,
                         `hash_v1`          char(40) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                         `hash_v2`          char(64) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                         `hash_v2_trunc`    char(40) COLLATE utf8mb4_general_ci              DEFAULT NULL,
                         `files`            int                                     NOT NULL,
                         `grabs`            int                                              DEFAULT 0,
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
    """
    Setup tables and perform migrations
    Creates a connection pool to MySQL database
    """
    # disable aiomysql useless warnings
    warnings.filterwarnings('ignore', module=r"aiomysql")

    global _db_pool
    tables = {"users": USERS_TABLE_SQL, "torrents": TORRENTS_TABLE_SQL, }

    # first connect as root to check/create the schema and give proper permissions to the runtime user
    async with aiomysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user="root", password=MYSQL_ROOT_PASSWORD, autocommit=True) as conn:
        async with conn.cursor() as cur:
            log.debug("[MYSQL] Connected to database as root for setup")

            # create the schema if doens't already exist
            await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            log.debug(f"[MYSQL] Ensured database '{MYSQL_DB}' exists")

            # create user if doesn't already exist
            await cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (MYSQL_USER, MYSQL_PASSWORD,))
            log.debug(f"[MYSQL] Ensured user '{MYSQL_USER}' exists")

            # grant permissions to user if not already
            await cur.execute(f"GRANT ALL ON `{MYSQL_DB}`.* TO %s@'%%'", (MYSQL_USER,))
            log.debug(f"[MYSQL] Granted privileges on '{MYSQL_DB}' to '{MYSQL_USER}'")

    # create the user connection pool
    _db_pool = await aiomysql.create_pool(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD, db=MYSQL_DB, autocommit=True)
    log.debug(f"[MYSQL] Connected to database '{MYSQL_DB}' as '{MYSQL_USER}'")

    # create any missing tables
    async with _db_pool.acquire() as conn:
        async with conn.cursor() as cur:
            for table_name, create_sql in tables.items():
                await cur.execute("SHOW TABLES LIKE %s", (table_name,))
                exists = await cur.fetchone()

                if not exists:
                    await cur.execute(create_sql)
                    log.info(f"[MYSQL] Created table '{table_name}'")

    log.debug("[MYSQL] Database setup completed")


async def disconnect_database():
    """
    Closes MySQL connection pool if active
    """
    if _db_pool is not None:
        _db_pool.close()
        await _db_pool.wait_closed()
        log.debug("[MYSQL] Connection pool closed")


async def _with_retry(fn, *args, **kwargs):
    """
    Retries failed MySQL queries up to MYSQL_MAX_RETY times
    """
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
    """
    Execute a query to MySQL and fetch all rows
    """

    async def _do():
        async with _db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()

    return await _with_retry(_do)


async def fetch_one(query: str, params: tuple = ()):
    """
    Execute a query to MySQL and fetch a single row
    """

    async def _do():
        async with _db_pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(query, params)
                return await cur.fetchone()

    return await _with_retry(_do)


async def execute(query: str, params: tuple = (), include_row_id: bool = False, include_row_count: bool = False):
    """
    Execute a query to MySQL and optionally fetch the row ID and modified row count
    """

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
