import logging
import os

log = logging.getLogger("privateindexer")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
log.setLevel(logging.getLevelName(LOG_LEVEL))
