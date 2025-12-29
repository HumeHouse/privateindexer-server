from datetime import datetime, timezone
import os
import re
from decimal import Decimal
from urllib.parse import unquote_to_bytes

import libtorrent as lt
from fastapi import Request

from privateindexer_server.core import mysql
from privateindexer_server.core.config import TORRENTS_DIR, CATEGORIES
from privateindexer_server.core.logger import log

SEASON_EPISODE_REGEX = re.compile(r"S(?P<season>\d{1,4})(?:E(?P<episode>\d{1,3}))?|(?P<season_alt>\d{1,4})x(?P<episode_alt>\d{1,3})", re.IGNORECASE, )


class User:

    def __init__(self, user_id: int, user_label: str, apikey: str, downloaded: int, uploaded: int):
        self.user_id: int = user_id
        self.user_label: str = user_label
        self.apikey: str = apikey
        self.downloaded: int = downloaded
        self.uploaded: int = uploaded


async def get_user_by_key(apikey: str) -> User | None:
    if not apikey:
        return None

    row = await mysql.fetch_one("SELECT id, label, downloaded, uploaded FROM users WHERE api_key = %s", (apikey,))
    if not row:
        return None

    return User(row["id"], row["label"], apikey, row["downloaded"], row["uploaded"])


def build_torrent_path(torrent_name: str) -> str:
    return os.path.join(TORRENTS_DIR, f"{torrent_name}.torrent")


def normalize_search_string(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().strip())


def extract_bt_param(raw_qs: bytes, key: str) -> bytes:
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
    return {category["id"]: category["name"] for category in CATEGORIES}.get(category_id)


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.2f} KiB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / (1024 ** 2):.2f} MiB"
    return f"{num_bytes / (1024 ** 3):.2f} GiB"


def time_ago(dt: datetime) -> str:
    if not dt:
        return "—"

    dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())

    if seconds < 0:
        return "just now"

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
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip

    return request.client.host


def get_torrent_hashes(torrent_file: str) -> tuple[str, str]:
    try:
        info = lt.torrent_info(torrent_file)
        hashes = info.info_hashes()

        return str(hashes.v1).lower(), str(hashes.v2).lower()
    except Exception as e:
        log.error(f"[TORRENT] Error getting hashes for '{torrent_file}': {e}")
        return "", ""


def find_matching_torrent(torrent_hash_v1: str, torrent_hash_v2: str) -> tuple[str | None, str]:
    found_match = None

    for torrent_file in os.listdir(TORRENTS_DIR):

        torrent_path = os.path.join(TORRENTS_DIR, torrent_file)
        try:
            hash_v1, hash_v2 = get_torrent_hashes(torrent_path)
            if hash_v1 == torrent_hash_v1 or hash_v2 == torrent_hash_v2:
                found_match = torrent_path
                break
        except Exception as e:
            log.error(f"[TORRENT] Error comparing hash for '{torrent_path}' to '{torrent_hash_v1}' / '{torrent_hash_v2}': {e}")
    return found_match, torrent_hash_v2


def extract_season_episode(name: str) -> tuple[int, int]:
    match = SEASON_EPISODE_REGEX.search(name)
    if not match:
        return None, None
    season = match.group("season") or match.group("season_alt")
    episode = match.group("episode") or match.group("episode_alt")
    return int(season) if season else None, int(episode) if episode else None
