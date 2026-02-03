import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import unquote_to_bytes

import libtorrent as lt
from unidecode import unidecode
from fastapi import Request

from privateindexer_server.core import redis
from privateindexer_server.core.config import TORRENTS_DIR, CATEGORIES, PEER_TIMEOUT
from privateindexer_server.core.logger import log

SEASON_EPISODE_REGEX = re.compile(r"S(?P<season>\d{1,4})(?:E(?P<episode>\d{1,3}))?|(?P<season_alt>\d{1,4})x(?P<episode_alt>\d{1,3})", re.IGNORECASE, )


def get_torrent_file(hash_v2: str) -> str:
    """
    Helper to concatenate torrents directory, torrent v2 hash, and torrent extension
    """
    return os.path.join(TORRENTS_DIR, f"{hash_v2}.torrent")


def clean_text_filter(text: str) -> str:
    """
    Helper to transliterate or remove invalid characters from text
    """
    # transliterate
    text = unidecode(text, replace_str="")
    text = text.lower().strip()

    # use a regex replacement to remove non-standard characters
    return re.sub(r"[^a-z0-9]+", "", text)


def extract_bt_param(raw_qs: bytes, key: str) -> bytes:
    """
    Helper function to pull Bittorrent query parameters from bytes
    """
    prefix = key.encode("ascii") + b"="
    start = raw_qs.find(prefix)
    if start == -1:
        return None
    start += len(prefix)
    end = raw_qs.find(b"&", start)
    if end == -1:
        end = len(raw_qs)
    raw_value = raw_qs[start:end]
    return unquote_to_bytes(raw_value.decode("ascii"))


def sanitize_bencode(obj):
    """
    Helper function to clean items within other bencoded items
    """
    if isinstance(obj, Decimal):
        return int(obj)
    elif isinstance(obj, dict):
        return {k: sanitize_bencode(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_bencode(v) for v in obj]
    elif isinstance(obj, tuple):
        return tuple(sanitize_bencode(v) for v in obj)
    return obj


def get_category_name(category_id: int) -> str | None:
    """
    Fetch a category name based on ID
    """
    return {category["id"]: category["name"] for category in CATEGORIES}.get(category_id)


def format_bytes(num_bytes: int) -> str:
    """
    Convert integer bytes into human-readable text in base 1024
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.2f} KiB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / (1024 ** 2):.2f} MiB"
    return f"{num_bytes / (1024 ** 3):.2f} GiB"


def time_ago(dt: datetime) -> str:
    """
    Convert a datetime to a human-readable 'x(value) y(unit) ago' format for time deltas
    """
    if not dt:
        return "—"

    dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds} seconds ago"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"

    days = hours // 24
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"

    return f"on {dt.strftime('%Y-%m-%d')}"


def get_client_ip(request: Request) -> str:
    """
    Helper to extract the IP address from a request
    """
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip

    return request.client.host


def get_torrent_hashes(torrent_file: str) -> tuple[str, str]:
    """
    Decode the hash v1 and v2 from the torrent info of a torrent file
    """
    try:
        info = lt.torrent_info(torrent_file)
        hashes = info.info_hashes()

        return str(hashes.v1).lower(), str(hashes.v2).lower()
    except Exception as e:
        log.error(f"[TORRENT] Error getting hashes for '{torrent_file}': {e}")
        return "", ""


def extract_season_episode(name: str) -> tuple[int, int]:
    """
    Helper function to find regex matches for season/episode numbers from the torrent name
    """
    match = SEASON_EPISODE_REGEX.search(name)
    if not match:
        return None, None
    season = match.group("season") or match.group("season_alt")
    episode = match.group("episode") or match.group("episode_alt")
    return int(season) if season else None, int(episode) if episode else None


async def get_seeders_and_leechers(torrent_id: int) -> tuple[int, int]:
    """
    Fetch seeders and leechers from Redis database for a torrent
    """
    redis_conn = redis.get_connection()
    now = int(time.time())
    cutoff = now - PEER_TIMEOUT

    seeders = leechers = 0

    peer_ids = await redis_conn.zrangebyscore(f"peers:{torrent_id}", min=cutoff, max=now)
    for pid in peer_ids:
        pdata = await redis_conn.hgetall(f"peer:{torrent_id}:{pid}")
        if not pdata:
            continue
        if int(pdata.get("left", 1)) == 0:
            seeders += 1
        else:
            leechers += 1

    return seeders, leechers


def generate_sid() -> str:
    """
    Generate a simple session ID
    """
    nonce = secrets.token_hex(16)
    raw = f"{nonce}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()
