import os

APP_VERSION = "1.4.0"

TORRENTS_DIR = "/app/torrents"

CATEGORIES = {"movies": {"id": 1000, "name": "Movies"}, "tv": {"id": 5000, "name": "TV"}}

ANNOUNCE_TRACKER_URL = "https://tracker.humehouse.com/announce"

PEER_TIMEOUT_INTERVAL = 60 * int(os.getenv("PEER_TIMEOUT_INTERVAL", 1))
PEER_TIMEOUT = int(os.getenv("PEER_TIMEOUT", 1800))

STATS_UPDATE_INTERVAL = int(os.getenv("STATS_UPDATE_INTERVAL", 30))

HIGH_LATECY_THRESHOLD = int(os.getenv("HIGH_LATECY_THRESHOLD", 250))

MAX_THREADS = int(os.getenv("MAX_THREADS", 48))

DATABASE_CHECK_INTERVAL = 60 * int(os.getenv("DATABASE_CHECK_INTERVAL", 120))

STALE_CHECK_INTERVAL = 60 * 60 * int(os.getenv("STALE_CHECK_INTERVAL", 12))
STALE_THRESHOLD = 60 * 60 * 24 * int(os.getenv("STALE_THRESHOLD", 30))

REDIS_HOST = os.getenv("REDIS_HOST")

MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB", "privateindexer")

MYSQL_MAX_RETY = 5
MYSQL_RETRY_BACKOFF = 0.2
