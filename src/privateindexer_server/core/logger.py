import logging
import os

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def channel(name: str):
    """
    Helper to obtain a logger for the specified channel
    """
    # get the base logger with channel name and set level
    base = logging.getLogger(f"privateindexer.{name.lower()}")
    base.setLevel(logging.getLevelName(LOG_LEVEL))

    return logging.LoggerAdapter(base, {"channel": name.upper()})
