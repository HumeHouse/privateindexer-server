import os

from privateindexer_server.core import logger

APP_VERSION = "1.11.7"

DATA_DIR = "/app/data"

TORRENTS_DIR = os.path.join(DATA_DIR, "torrents")

JWT_KEY_FILE = os.path.join(DATA_DIR, "jwt.key")

ADMIN_PASSWORD_FILE = os.path.join(DATA_DIR, "admin.password")

CATEGORIES = [{"id": 2000, "name": "Movies"}, {"id": 5000, "name": "TV"}, {"id": 3000, "name": "Audio"}]

EXTERNAL_SERVER_URL = (os.getenv("EXTERNAL_SERVER_URL", "")).strip("/")

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


def validate_environment():
    """
    Check environment variables for validity and exit on errors
    """
    logger.channel("config").info("Validating environment")

    # check if data directory exists
    if not os.path.isdir(DATA_DIR):
        logger.channel("config").critical(f"Data directory does not exist: {DATA_DIR}")
        exit(1)

    # check if data directory has correct permissions
    try:
        test_file = os.path.join(DATA_DIR, ".write_test")
        with open(test_file, "w"):
            pass
        os.unlink(test_file)
    except OSError:
        logger.channel("config").critical(f"Data directory is not writable: {DATA_DIR}")
        exit(1)

    # try to create torrents directory
    try:
        os.makedirs(TORRENTS_DIR, exist_ok=True)
    except Exception as e:
        logger.channel("config").exception(f"Exception while creating torrent data directory: {e}")
        exit(1)

    # ensure server URL set
    if not EXTERNAL_SERVER_URL:
        logger.channel("config").critical(f"No external server URL set")
        exit(1)

    # ensure Redis server host is set
    if not REDIS_HOST:
        logger.channel("config").critical(f"No Redis server host set")
        exit(1)

    # ensure MySQL host is set
    if not MYSQL_HOST:
        logger.channel("config").critical(f"No MySQL server host set")
        exit(1)

    # ensure MySQL root password is set
    if not MYSQL_ROOT_PASSWORD:
        logger.channel("config").critical(f"No MySQL root password set")
        exit(1)

    logger.channel("config").info("Environment is valid")
