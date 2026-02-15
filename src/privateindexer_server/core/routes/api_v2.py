import itertools
import os
import re
import shutil
import socket
import tempfile
from collections import defaultdict

import libtorrent as lt
from fastapi import HTTPException, Query, Request, UploadFile, File, Form, APIRouter, Depends
from fastapi.responses import Response, PlainTextResponse, JSONResponse

from privateindexer_server.core import logger
from privateindexer_server.core import mysql, utils, redis, route_helper
from privateindexer_server.core.config import CATEGORIES, SYNC_BATCH_SIZE
from privateindexer_server.core.jwt_helper import AccessTokenValidator
from privateindexer_server.core.route_helper import api_key_required, latency_threshold
from privateindexer_server.core.user_helper import User

router = APIRouter(prefix="/api/v2")


@router.get("/health")
def get_health():
    """
    Endpoint to be used by Docker for checking the readiness of the API
    """
    return PlainTextResponse("OK")


@router.get("/analytics", dependencies=[Depends(latency_threshold(1000))])
async def get_analytics(user: User = Depends(api_key_required)):
    """
    Called by tracking apps like Zabbix to obtain information about the status of the server
    """
    logger.channel("analytics").debug(f"User '{user.user_label}' requested analytics")
    try:
        redis_connection = redis.get_connection()

        # fetch basic stats from Redis
        requests = int((await redis_connection.get("stats:requests")) or 0)
        bytes_sent = int((await redis_connection.get("stats:bytes_sent")) or 0)
        bytes_received = int((await redis_connection.get("stats:bytes_received")) or 0)
        unique_visitors = await redis_connection.scard("stats:unique_ips")

        # process the request times list and normalize negative values
        times_raw = await redis_connection.lrange("stats:request_times", -1000, -1)
        times = [float(t) for t in times_raw] if times_raw else []

        # convert the request times into three values - min, max, and average
        request_time_avg = (sum(times) / len(times)) / 1000 if times else 0.0
        request_time_min = min(times) / 1000 if times else 0.0
        request_time_max = max(times) / 1000 if times else 0.0

        # obtain all peer keys from Redis with a looping cursor to prevent locking the Redis server with huge numbers of peers
        peer_keys = []
        cursor = 0
        while True:
            cursor, keys = await redis_connection.scan(cursor=cursor, match="peer:*:*", count=1000)
            peer_keys.extend(keys)
            if cursor == 0:
                break

        # fetch all peer hashes in one go using a pipeline
        pipe = redis_connection.pipeline()
        for peer_key in peer_keys:
            await pipe.hgetall(peer_key)
        all_peers_data = await pipe.execute()

        # aggregate each peer with its mapped data to each torrent which it belongs to
        torrents = defaultdict(lambda: {"seeders": 0, "leechers": 0})
        for peer_key, peer_data in zip(peer_keys, all_peers_data):
            # skip invalid peer data
            if not peer_data:
                continue

            # parse torrent_id from key "peer:{torrent_id}:{peer_id}"
            _, torrent_id, _ = peer_key.split(":")
            torrent_id = int(torrent_id)

            # increment seeders/leechers based on peer's peice data left to obtain
            left = int(peer_data.get("left", 1))
            if left == 0:
                torrents[torrent_id]["seeders"] += 1
            else:
                torrents[torrent_id]["leechers"] += 1

        # add all the aggregated data up to gather totals
        total_peers = sum(v["seeders"] + v["leechers"] for v in torrents.values())
        seeding_torrents = sum(1 for v in torrents.values() if v["seeders"])
        leeching_torrents = sum(1 for v in torrents.values() if v["leechers"])

    except Exception as e:
        logger.channel("analytics").exception(f"Failed to get analytics from Redis: {e}")
        return JSONResponse({})

    # fetch all user data transfer statistics
    data_transfer = await mysql.fetch_one("SELECT SUM(downloaded) AS total_downloaded, SUM(uploaded) AS total_uploaded FROM users")
    total_downloaded = int(data_transfer["total_downloaded"] or 0)
    total_uploaded = int(data_transfer["total_uploaded"] or 0)

    # gather various stats from the torrents table
    torrent_metrics = await mysql.fetch_one("SELECT COUNT(*) as total_torrents, SUM(grabs) as grabs FROM torrents")
    total_torrents = int(torrent_metrics.get("total_torrents", 0))
    grabs_total = int(torrent_metrics.get("grabs") or 0)

    analytics = {"requests": int(requests), "bytes_sent": int(bytes_sent), "bytes_received": int(bytes_received), "unique_visitors": unique_visitors,
                 "total_torrents": total_torrents, "seeding_torrents": seeding_torrents, "leeching_torrents": leeching_torrents, "total_peers": total_peers,
                 "total_grabs": grabs_total, "total_downloaded": total_downloaded, "total_uploaded": total_uploaded, "request_time_avg": request_time_avg,
                 "request_time_min": request_time_min, "request_time_max": request_time_max, }

    return JSONResponse(analytics)


@router.get("/user")
async def user_login_check(user: User = Depends(api_key_required), request: Request = None, v: str = Query(...), announce_ip: str = Query(None), port: int = Query(None),
                           public_uploads: bool = Query(...)):
    """
    Called by PrivateIndexer clients during startup to validate the API key and update the server with preferences/stats
    """
    # use the provided IP and port, otherwise fallback to request IP and default port
    announce_ip = announce_ip or route_helper.get_client_ip(request)
    port = port or 6881

    # check to see if the client is reachable at the IP and port they sent us
    reachable = False
    try:
        with socket.create_connection((announce_ip, port), timeout=5):
            reachable = True
            logger.channel("user").info(f"User '{user.user_label}' ({announce_ip}:{port}) connected with PrivateIndexer client v{v}")
    except (socket.timeout, ConnectionRefusedError, OSError):
        logger.channel("user").warning(f"User '{user.user_label}' ({announce_ip}:{port} - UNREACHABLE) connected with PrivateIndexer client v{v}")
        pass

    # update the user's entry with the data
    await mysql.execute("UPDATE users SET client_version = %s, last_ip = %s, last_seen=NOW(), reachable = %s, public_uploads = %s WHERE id = %s",
                        (v, f"{announce_ip}:{port}", reachable, public_uploads, user.user_id))

    user_data = {"user_label": user.user_label, "announce_ip": announce_ip, "is_reachable": reachable, }
    return JSONResponse(user_data)


@router.get("/user/stats")
async def get_user_stats(user: User = Depends(api_key_required)):
    """
    Called by users (on PrivateIndexer clients) to obtain their stats from the server side
    """
    logger.channel("user").debug(f"User '{user.user_label}' requested statistics")

    user_id = user.user_id

    # pull all the user stats from the database
    stats_query = "SELECT torrents_uploaded, grabs, popularity, downloaded, uploaded, seeding, leeching FROM users WHERE id = %s"
    stats = await mysql.fetch_one(stats_query, (user_id,))

    torrents_uploaded = int(stats["torrents_uploaded"] or 0)
    grabs = int(stats["grabs"] or 0)
    popularity = int(stats["popularity"] or 0)
    downloaded = stats["downloaded"] or 0
    uploaded = stats["uploaded"] or 0
    seeding = int(stats["seeding"] or 0)
    leeching = int(stats["leeching"] or 0)

    # calculate the ratio from data transfer stats
    if downloaded > 0:
        server_ratio = uploaded / downloaded
    elif uploaded > 0:
        server_ratio = 8640000
    else:
        server_ratio = 0.0

    return JSONResponse(
        {"user": user.user_label, "torrents_added_total": torrents_uploaded, "currently_seeding": seeding, "currently_leeching": leeching, "grabs_total": grabs,
         "popularity": popularity, "total_download": downloaded, "total_upload": uploaded, "server_ratio": server_ratio, })


@router.get("/grab")
async def grab(user: User = Depends(AccessTokenValidator("grab")), infohash: str = Query(...)):
    """
    Called by a client to request a torrent file which matches the provided infohash
    """
    # search for the infohash in the database
    torrent = await mysql.fetch_one("SELECT id, hash_v2 FROM torrents WHERE hash_v2 = %s LIMIT 1", (infohash,))
    if not torrent:
        logger.channel("grab").debug(f"User '{user.user_label}' tried to grab invalid torrent with hash '{infohash}'")
        raise HTTPException(status_code=404, detail="Torrent not found")

    hash_v2 = torrent["hash_v2"]

    # ensure the torrent file exists on disk
    torrent_file = utils.get_torrent_file(hash_v2)
    if not os.path.exists(torrent_file):
        logger.channel("grab").critical(f"Torrent file missing for hash {infohash}")
        raise HTTPException(status_code=404, detail="Torrent file missing")

    torrent_filename = os.path.basename(torrent_file)

    # attempt to read the file
    try:
        with open(torrent_file, "rb") as f:
            bencoded = f.read()
    except Exception as e:
        logger.channel("grab").exception(f"Failed to read torrent with hash '{infohash}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    # increment the torrent grab counter
    await mysql.execute("UPDATE torrents SET grabs = grabs + 1 WHERE id=%s", (torrent["id"],))

    # increment the user grab counter
    await mysql.execute("UPDATE users SET grabs = grabs + 1 WHERE id=%s", (user.user_id,))

    logger.channel("grab").info(f"User '{user.user_label}' grabbed torrent by hash '{infohash}'")

    # reply with the bencoded response over x-bittorrent protocol
    return Response(content=bencoded, media_type="application/x-bittorrent", headers={"Content-Disposition": f'attachment; filename="{torrent_filename}"'})


@router.get("/validate")
async def validate(user: User = Depends(api_key_required), infohash: str = Query(...)):
    """
    Called by a client to validate if an infohash exists on the server
    """
    # search for the infohash in the database
    torrent = await mysql.fetch_one("SELECT id FROM torrents WHERE hash_v2 = %s LIMIT 1", (infohash,))
    if not torrent:
        logger.channel("validate").debug(f"User '{user.user_label}' tried to validate torrent with invalid hash '{infohash}'")
        raise HTTPException(status_code=404, detail="Torrent not found")

    logger.channel("validate").info(f"User '{user.user_label}' successfully validated torrent by hash '{infohash}'")

    # reply with success message
    return PlainTextResponse("Torrent is valid")


@router.post("/upload")
async def upload(user: User = Depends(api_key_required), category: int = Form(...), torrent_file: UploadFile = File(...), torrent_name: str = Form(...),
                 imdbid: str = Form(None), tmdbid: int = Form(None), tvdbid: int = Form(None), artist: str = Form(None), album: str = Form(None)):
    """
    Called by a client to upload a torrent file along with various metadata to be stored on teh server
    """
    user_id = user.user_id
    user_label = user.user_label

    # ensure the client is using a valid torznab category
    category_id_list = [cat["id"] for cat in CATEGORIES]
    if category not in category_id_list:
        logger.channel("upload").warning(f"User '{user_label}' tried to upload with invalid category: {category}")
        raise HTTPException(status_code=400, detail="Invalid category")

    # make sure this is actually a torrent file
    if not torrent_file.filename.endswith(".torrent"):
        logger.channel("upload").warning(f"User '{user_label}' tried to upload non-torrent file: {torrent_file.filename}")
        raise HTTPException(status_code=400, detail="File must be torrent file")

    # save the data to a temporary file
    temporary_download_file = tempfile.NamedTemporaryFile()
    temporary_download_file.write(await torrent_file.read())
    torrent_download_path = temporary_download_file.name

    try:
        # get the infodata from the torrent file
        info = lt.torrent_info(torrent_download_path)

        # try remove any trackers
        if len(list(info.trackers())) > 0:
            try:
                info.clear_trackers()
            except Exception:
                pass

        # strip all invalid characters from the torrent name
        normalized_torrent_name = utils.clean_text_filter(torrent_name)

        file_count = len(info.files())
        size = info.total_size()
        hash_v1, hash_v2 = utils.get_torrent_hashes(torrent_download_path)

        # truncate the v2 hash for quick torrent announcement matching
        hash_v2_truncated = hash_v2[:40]

        # check to see if we can pull a season/episode number from the torrent name
        season_match, episode_match = utils.extract_season_episode(torrent_name)
    except Exception as e:
        os.unlink(torrent_download_path)
        logger.channel("upload").exception(f"Failed to process torrent file sent by '{user_label}': '{torrent_file.filename}': {e}")
        raise HTTPException(status_code=400, detail="Invalid torrent file")

    # add optional indexing parameters
    if imdbid:
        imdbid = int(re.sub(r"\D", "", imdbid))

    if artist:
        artist = utils.clean_text_filter(artist)

    if album:
        album = utils.clean_text_filter(album)

    # check to see if this torrent already exists in the database
    existing = await mysql.fetch_one("SELECT id, name, added_by_user_id FROM torrents WHERE hash_v1=%s OR hash_v2=%s", (hash_v1, hash_v2))
    if existing:

        # if the torrent exists and this user was the original uploader, overwrite the old metadata with the new
        if existing["added_by_user_id"] == user_id:
            await mysql.execute(
                "UPDATE torrents SET name = %s, normalized_name = %s, hash_v1 = %s, hash_v2 = %s, hash_v2_trunc = %s, season = %s, episode = %s, imdbid = %s, tmdbid = %s, tvdbid = %s, artist = %s, album = %s, last_seen = NOW() WHERE id = %s",
                (torrent_name, normalized_torrent_name, hash_v1, hash_v2, hash_v2_truncated, season_match, episode_match, imdbid, tmdbid, tvdbid, artist, album,
                 existing["id"]))
            logger.channel("upload").info(f"User '{user_label}' re-uploaded torrent, renamed to '{torrent_name}'")

        # ignore the upload if this user was no the original uploader
        else:
            logger.channel("upload").debug(f"User '{user_label}' uploaded duplicate torrent: '{torrent_name}'")

        # delete the temporary file
        os.unlink(torrent_download_path)
        raise HTTPException(status_code=409, detail="Torrent with same hash exists, updated name in database")

    # move the temporary file to the permanent torrent storage directory
    torrent_save_path = utils.get_torrent_file(hash_v2)
    shutil.move(torrent_download_path, torrent_save_path)

    # remove dangling temporary torrent file
    if os.path.exists(torrent_download_path):
        os.unlink(torrent_download_path)

    # add the final metadata to the database
    await mysql.execute("""
                        INSERT INTO torrents (name, normalized_name, season, episode, imdbid, tmdbid, tvdbid, artist, album, size, category, hash_v1,
                                              hash_v2, hash_v2_trunc, files, added_on, added_by_user_id, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW())
                        """,
                        (torrent_name, normalized_torrent_name, season_match, episode_match, imdbid, tmdbid, tvdbid, artist, album, size, category,
                         hash_v1, hash_v2, hash_v2_truncated, file_count, user_id))

    logger.channel("upload").info(f"User '{user_label}' uploaded torrent '{torrent_name}'")

    return PlainTextResponse("Successfully uploaded torrent")


@router.post("/sync", dependencies=[Depends(latency_threshold(5000))])
async def sync(user: User = Depends(api_key_required), request: Request = None):
    """
    Called by clients with their list of tracked torrents including local ID, infohash, and torrent name to sync with the server database
    """
    torrents: list[dict[str, int | str]] = await request.json()

    # loop through each torrent
    rows = []
    for t in torrents:
        infohash = t.get("infohash")
        torrent_name = t.get("name")
        # remove invalid characters from the torrent name
        normalized_torrent_name = utils.clean_text_filter(torrent_name) if torrent_name else None

        # only add valid infohashes to the search
        if infohash:
            rows.append((t["id"], infohash, torrent_name, normalized_torrent_name))

    # if none of the sent rows were valid, return a mirrored response
    if not rows:
        return JSONResponse({"missing_ids": [t["id"] for t in torrents]})

    missing_ids: list[int] = []

    # create batches of torrents to search for in the database to reduce large queries
    for batch in itertools.batched(rows, SYNC_BATCH_SIZE):
        selects = []
        params = []

        # loop through each torrent and add it to the union select query
        for torrent_id, infohash, torrent_name, normalized_torrent_name in batch:
            selects.append("SELECT %s AS id, %s AS infohash, %s AS name, %s AS normalized_name")
            params.extend([torrent_id, infohash, torrent_name, normalized_torrent_name])

        # assemble the union table selects into one large query
        union_sql = " UNION ALL ".join(selects)

        # create the final query to find torrents which already exist in the database
        missing_query = f"""
                    SELECT c.id
                    FROM (
                        {union_sql}
                    ) AS c
                    LEFT JOIN torrents t
                      ON t.hash_v2 = c.infohash
                    WHERE
                        t.hash_v2 IS NULL
                        OR (t.hash_v1 IS NULL AND t.added_by_user_id = %s)
                """

        result = await mysql.fetch_all(missing_query, params + [user.user_id])
        missing_ids.extend(row["id"] for row in result)

        # reset the name and normalized name in the database to match what the client sent us (only if they are original uploader)
        update_query = f"""
                    UPDATE torrents t
                    JOIN (
                        {union_sql}
                    ) AS c
                      ON c.infohash = t.hash_v2
                    SET t.name = c.name, t.normalized_name = c.normalized_name
                    WHERE
                        c.name IS NOT NULL AND c.normalized_name IS NOT NULL AND t.added_by_user_id = %s
                        AND (t.name != c.name OR t.normalized_name != c.normalized_name)
                """

        await mysql.execute(update_query, params + [user.user_id])

    logger.channel("sync").debug(f"User '{user.user_label}' performed sync: {len(missing_ids)} missing (sent {len(torrents)})")

    # reply with a list of local torrent IDs that are not currently tracked in the server database
    return JSONResponse({"missing_ids": missing_ids})
