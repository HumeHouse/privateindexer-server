import os

APP_VERSION = "1.11.2"

DATA_DIR = "/app/data"

TORRENTS_DIR = os.path.join(DATA_DIR, "torrents")

JWT_KEY_FILE = os.path.join(DATA_DIR, "jwt.key")

ADMIN_PASSWORD_FILE = os.path.join(DATA_DIR, "admin.password")

CATEGORIES = [{"id": 2000, "name": "Movies"}, {"id": 5000, "name": "TV"}, {"id": 3000, "name": "Audio"}]

# TODO: deprecated - remove in upcoming release
EXTERNAL_TRACKER_URL = (os.getenv("EXTERNAL_TRACKER_URL")).strip("/")
ANNOUNCE_TRACKER_URL = f"{EXTERNAL_TRACKER_URL}/announce"

EXTERNAL_SERVER_URL = (os.getenv("EXTERNAL_SERVER_URL")).strip("/")

PEER_TIMEOUT_INTERVAL = 60 * int(os.getenv("PEER_TIMEOUT_INTERVAL", 1))
PEER_TIMEOUT = int(os.getenv("PEER_TIMEOUT", 1800))

STATS_UPDATE_INTERVAL = int(os.getenv("STATS_UPDATE_INTERVAL", 30))

HIGH_LATECY_THRESHOLD = int(os.getenv("HIGH_LATECY_THRESHOLD", 250))

DATABASE_CHECK_INTERVAL = 60 * 60 * int(os.getenv("DATABASE_CHECK_INTERVAL", 12))

CLIENT_CHECK_INTERVAL = 60 * int(os.getenv("CLIENT_CHECK_INTERVAL", 15))

STALE_CHECK_INTERVAL = 60 * 60 * int(os.getenv("STALE_CHECK_INTERVAL", 6))
STALE_THRESHOLD = 60 * 60 * 24 * int(os.getenv("STALE_THRESHOLD", 30))

SYNC_BATCH_SIZE = int(os.getenv("SYNC_BATCH_SIZE", 5000))

ACCESS_TOKEN_EXPIRATION = int(os.getenv("ACCESS_TOKEN_EXPIRATION", 10))

SITE_NAME = os.getenv("SITE_NAME", "HumeHouse PrivateIndexer")

REDIS_HOST = os.getenv("REDIS_HOST")

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_ROOT_PASSWORD = os.getenv("MYSQL_ROOT_PASSWORD")
MYSQL_USER = os.getenv("MYSQL_USER", "privateindexer")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "privateindexer")
MYSQL_DB = os.getenv("MYSQL_DB", "privateindexer")

MYSQL_MAX_RETY = 5
MYSQL_RETRY_BACKOFF = 0.2
