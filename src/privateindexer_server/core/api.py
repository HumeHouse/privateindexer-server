import datetime
import os
import shutil
import socket
import tempfile
from xml.sax.saxutils import escape

import libtorrent as lt
from fastapi import HTTPException, Query, Request, UploadFile, File, Form, APIRouter, Depends, Header
from fastapi.responses import Response, PlainTextResponse, JSONResponse

from privateindexer_server.core import mysql, utils, redis
from privateindexer_server.core.config import CATEGORIES, PEER_TIMEOUT, ANNOUNCE_TRACKER_URL
from privateindexer_server.core.logger import log
from privateindexer_server.core.utils import User

router = APIRouter()


async def api_key_required(apikey_query: str | None = Query(None, alias="apikey"), apikey_form: str | None = Form(None, alias="apikey"),
                           apikey_header: str | None = Header(None, alias="X-API-Key"), ) -> User:
    apikey = apikey_query or apikey_form or apikey_header

    if not apikey:
        raise HTTPException(status_code=401, detail="API key missing")

    user = await utils.get_user_by_key(apikey)
    if not user:
        log.warning(f"[USER] Invalid API key sent: {apikey}")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


@router.get("/health")
def get_health():
    """
    Endpoint to be used by Docker for checking the readiness of the API
    """
    return PlainTextResponse("OK")


@router.get("/analytics")
async def get_stats(user: User = Depends(api_key_required)):
    log.debug(f"[ANALYTICS] User '{user.user_label}' requested analytics")
    try:
        redis_connection = redis.get_connection()
        requests = redis_connection.get("stats:requests") or 0
        bytes_sent = redis_connection.get("stats:bytes_sent") or 0
        bytes_received = redis_connection.get("stats:bytes_received") or 0
        unique_visitors = redis_connection.scard("stats:unique_ips")
        request_times_raw = redis_connection.lrange("stats:request_times", -1000, -1)
    except Exception as e:
        log.error(f"[ANALYTICS] Failed to get analytics from Redis: {e}")
        return JSONResponse({})

    request_times = [float(x) for x in request_times_raw] if request_times_raw else []
    request_time_avg = (sum(request_times) / len(request_times)) / 1000 if request_times else 0.0
    request_time_max = max(request_times) / 1000 if request_times else 0.0
    request_time_min = min(request_times) / 1000 if request_times else 0.0

    data_transfer = await mysql.fetch_one("SELECT SUM(downloaded) AS total_downloaded, SUM(uploaded) AS total_uploaded FROM users")
    total_downloaded = int(data_transfer["total_downloaded"] or 0)
    total_uploaded = int(data_transfer["total_uploaded"] or 0)

    torrent_metrics = await mysql.fetch_one("SELECT COUNT(*) as total_torrents, SUM(grabs) as grabs FROM torrents")
    total_torrents = int(torrent_metrics.get("total_torrents", 0))
    grabs_total = int(torrent_metrics.get("grabs") or 0)

    seed_leech_query = """
                       SELECT SUM(IF(left_bytes = 0, 1, 0))                                AS seeding_peers, \
                              SUM(IF(left_bytes > 0, 1, 0))                                AS leeching_peers, \
                              COUNT(DISTINCT CASE WHEN left_bytes = 0 THEN torrent_id END) AS seeding_torrents, \
                              COUNT(DISTINCT CASE WHEN left_bytes > 0 THEN torrent_id END) AS leeching_torrents, \
                              COUNT(*)                                                     AS peers_total
                       FROM peers
                       WHERE last_seen > NOW() - INTERVAL %s SECOND; \
                       """
    seed_leech = await mysql.fetch_one(seed_leech_query, (int(PEER_TIMEOUT),))
    seeding_torrents = int(seed_leech.get("seeding_torrents") or 0)
    leeching_torrents = int(seed_leech.get("leeching_torrents") or 0)
    peers_total = int(seed_leech.get("peers_total") or 0)

    analytics = {"requests": int(requests), "bytes_sent": int(bytes_sent), "bytes_received": int(bytes_received), "unique_visitors": unique_visitors,
                 "total_torrents": total_torrents, "seeding_torrents": seeding_torrents, "leeching_torrents": leeching_torrents, "total_peers": peers_total,
                 "total_grabs": grabs_total, "total_downloaded": total_downloaded, "total_uploaded": total_uploaded, "request_time_avg": request_time_avg,
                 "request_time_min": request_time_min, "request_time_max": request_time_max, }

    return JSONResponse(analytics)


@router.get("/user")
async def current_user(user: User = Depends(api_key_required), request: Request = None, v: str = Query(...), announce_ip: str = Query(None), port: int = Query(None)):
    announce_ip = announce_ip or utils.get_client_ip(request)
    port = port or 6881

    reachable = False
    try:
        with socket.create_connection((announce_ip, port), timeout=5):
            reachable = True
            log.warning(f"[USER] User '{user.user_label}' ({announce_ip}:{port} - UNREACHABLE) connected with PrivateIndexer client v{v}")
    except (socket.timeout, ConnectionRefusedError, OSError):
        log.info(f"[USER] User '{user.user_label}' ({announce_ip}:{port}) connected with PrivateIndexer client v{v}")
        pass

    await mysql.execute("UPDATE users SET client_version = %s, last_ip = %s, last_seen=NOW(), reachable = %s WHERE id=%s",
                        (v, f"{announce_ip}:{port}", reachable, user.user_id,))

    user_data = {"user_label": user.user_label, "announce_ip": announce_ip, "is_reachable": reachable, }
    return JSONResponse(user_data)


@router.get("/user/stats")
async def get_user_stats(user: User = Depends(api_key_required)):
    log.debug(f"[USER] User '{user.user_label}' requested statistics")

    user_id = user.user_id

    stats_query = """
                  SELECT (SELECT COUNT(*) FROM torrents WHERE added_by_user_id = %s)   AS torrents_added_total, \
                         (SELECT SUM(grabs) FROM torrents WHERE added_by_user_id = %s) AS grabs_total, \
                         (SELECT downloaded FROM users WHERE id = %s)                  AS downloaded, \
                         (SELECT uploaded FROM users WHERE id = %s)                    AS uploaded \
                  """
    stats = await mysql.fetch_one(stats_query, (user_id, user_id, user_id, user_id))

    torrents_added_total = int(stats["torrents_added_total"] or 0)
    grabs_total = int(stats["grabs_total"] or 0)
    total_downloaded = stats["downloaded"] or 0
    total_uploaded = stats["uploaded"] or 0

    seed_leech_query = """
                       SELECT SUM(is_seed) AS seeding, SUM(is_leech) AS leeching
                       FROM (SELECT p.torrent_id, \
                                    MAX(IF(p.left_bytes = 0, 1, 0)) AS is_seed, \
                                    MAX(IF(p.left_bytes > 0, 1, 0)) AS is_leech \
                             FROM peers p \
                             WHERE p.user_id = %s \
                               AND p.last_seen > NOW() - INTERVAL %s SECOND \
                             GROUP BY p.torrent_id) AS t \
                       """
    seed_leech = await mysql.fetch_one(seed_leech_query, (user_id, PEER_TIMEOUT))
    currently_seeding = int(seed_leech["seeding"] or 0)
    currently_leeching = int(seed_leech["leeching"] or 0)

    if total_downloaded > 0:
        server_ratio = total_uploaded / total_downloaded
    elif total_uploaded > 0:
        server_ratio = 8640000
    else:
        server_ratio = 0.0

    # TODO: remove deprecated key `peers_on_user_torrents`
    user_stats = {"user": user.user_label, "torrents_added_total": torrents_added_total, "currently_seeding": currently_seeding, "currently_leeching": currently_leeching,
                  "peers_on_user_torrents": 0, "grabs_total": grabs_total, "total_download": total_downloaded, "total_upload": total_uploaded,
                  "server_ratio": server_ratio}

    return JSONResponse(user_stats)


@router.get("/api")
async def torznab_api(user: User = Depends(api_key_required), t: str = Query(...), q: str = Query(""), cat: str = Query(None), season: int = Query(None),
                      ep: int = Query(None), imdbid: str = Query(None), tmdbid: int = Query(None), limit: int = Query(100), offset: int = Query(0)):
    if t == "caps":
        log.debug(f"[TORZNAB] User '{user.user_label}' sent capability request")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <caps>
            <server version="1.0" title="HumeHouse PrivateIndexer Server"/>
            <limits default="100" max="1000"/>
            <categories>
            {''.join([f'<category id="{c["id"]}" name="{c["name"]}"/>' for c in CATEGORIES.values()])}
            </categories>
            <searching>
                <search available="yes" supportedParams="q"/>
                <tv-search available="yes" supportedParams="q,season,ep,imdbid,tmdbid"/>
                <movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>
                <music-search available="no"/>
                <audio-search available="no"/>
                <book-search available="no"/>
            </searching>
        </caps>"""
        return Response(content=xml, media_type="application/xml")

    elif t in ["search", "tvsearch", "moviesearch"]:
        limit = min(int(limit), 1000)
        before = datetime.datetime.now()

        where_clauses = []
        params = []

        if q is not None:
            normalized_q = f"%{utils.normalize_search_string(q).lower()}%"
            where_clauses.append("t.normalized_name LIKE %s")
            params.append(normalized_q)

        if cat is not None:
            cats = [int(c) for c in cat.split(",")]
            where_clauses.append(f"t.category IN ({",".join(["%s"] * len(cats))})")
            params.extend(cats)

        if t == "tvsearch":
            if season is not None:
                where_clauses.append("t.season = %s")
                params.append(int(str(season).lstrip("0")))
            if ep is not None:
                where_clauses.append("t.episode = %s")
                params.append(int(str(ep).lstrip("0")))
        elif t == "moviesearch":
            where_clauses.append("t.season = NULL")
            where_clauses.append("t.episode = NULL")

        if imdbid is not None:
            where_clauses.append("t.imdbid = %s")
            params.append(imdbid)

        if tmdbid is not None:
            where_clauses.append("t.tmdbid = %s")
            params.append(tmdbid)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        query = f"""
            SELECT *, COUNT(*) OVER() AS total_matches
            FROM (
                SELECT t.*,
                       COALESCE(SUM(IF(p.left_bytes = 0, 1, 0)), 0) AS seeders,
                       COALESCE(SUM(IF(p.left_bytes > 0, 1, 0)), 0) AS leechers
                FROM torrents t
                LEFT JOIN peers p
                       ON t.id = p.torrent_id
                      AND p.last_seen > NOW() - INTERVAL %s SECOND
                WHERE {where_sql}
                GROUP BY t.id
            ) AS sub
            ORDER BY seeders DESC
            LIMIT %s OFFSET %s
        """

        query_params = (int(PEER_TIMEOUT),) + tuple(params) + (int(limit), int(offset))
        results = await mysql.fetch_all(query, query_params)
        total_matches = results[0]["total_matches"] if results else 0

        delta = datetime.datetime.now() - before
        query_duration = f"{round(delta.total_seconds() * 1000)} ms"
        search_params = {
            "cat": cat,
            "season": season,
            "ep": ep,
            "imdbid": imdbid,
            "tmdbid": tmdbid,
        }
        search_params = ",".join(f"{k}={v}" for k, v in search_params.items() if v is not None)
        log.info(
            f"[TORZNAB] User '{user.user_label}' searched '{q}' with params {search_params} ({query_duration}): "
            f"returned {len(results)} results, found {total_matches} total"
        )

        items = []
        for t_entry in results:
            seeders = t_entry["seeders"]
            leechers = t_entry["leechers"]

            torrent_url_with_key = f"https://indexer.humehouse.com/grab?hash_v2={t_entry['hash_v2']}&apikey={user.apikey}"
            item = f"""
            <item>
              <title>{escape(t_entry["name"])}</title>
              <guid isPermaLink="false">humehouse-{t_entry['hash_v2']}</guid>
              <link>{escape(torrent_url_with_key)}</link>
              <enclosure url="{escape(torrent_url_with_key)}" length="{t_entry["size"]}" type="application/x-bittorrent"/>
              <size>{t_entry["size"]}</size>
              <pubDate>{t_entry["added_on"].strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate>
              <category>{t_entry["category"]}</category>
              <torznab:attr name="category" value="{t_entry["category"]}" />
              <torznab:attr name="files" value="{t_entry['files']}"/>
              <torznab:attr name="seeders" value="{seeders}"/>
              <torznab:attr name="leechers" value="{leechers}"/>
              <torznab:attr name="peers" value="{seeders + leechers}"/>
              <torznab:attr name="grabs" value="{t_entry['grabs']}"/>
              <torznab:attr name="infohash" value="{t_entry['hash_v2']}"/>
              {f"<torznab:attr name=\"imdbid\" value=\"{t_entry["imdbid"]}\"/>" if t_entry.get("imdbid") else ""}
              {f"<torznab:attr name=\"tmdbid\" value=\"{t_entry["tmdbid"]}\"/>" if t_entry.get("tmdbid") else ""}
              {f"<torznab:attr name=\"season\" value=\"{t_entry["season"]}\"/>" if t_entry.get("season") else ""}
              {f"<torznab:attr name=\"episode\" value=\"{t_entry["episode"]}\"/>" if t_entry.get("episode") else ""}
            </item>
            """
            items.append(item)

        xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
        <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
          <channel>
            <title>HumeHouse Private Indexer</title>
            <description>For friends of David</description>
            <link>https://indexer.humehouse.com/api</link>
            <torznab:response offset="{offset}" total="{total_matches}"/>
            {"".join(items)}
          </channel>
        </rss>"""
        return Response(content=xml, media_type="application/xml")

    else:
        raise HTTPException(status_code=400, detail="Unsupported request type")


@router.get("/grab")
async def grab(user: User = Depends(api_key_required), hash_v1: str = Query(None), hash_v2: str = Query(None)):
    if not hash_v1 and not hash_v2:
        raise HTTPException(status_code=422, detail="Specify one of either hash_v1 or hash_v2")

    hash_to_search = (hash_v2 or hash_v1).lower()

    torrent = await mysql.fetch_one("SELECT * FROM torrents WHERE hash_v1=%s OR hash_v2 LIKE %s LIMIT 1", (hash_to_search, f"{hash_to_search}%"))
    if not torrent:
        log.debug(f"[GRAB] User '{user.user_label}' tried to grab invalid torrent with hash '{hash_to_search}'")
        raise HTTPException(status_code=404, detail="Torrent not found")

    torrent_path = torrent["torrent_path"]
    if not os.path.exists(torrent_path):
        log.error(f"[GRAB] Torrent file missing for hash {hash_to_search}")
        raise HTTPException(status_code=404, detail="Torrent file missing")

    torrent_filename = os.path.basename(torrent_path)

    try:
        with open(torrent_path, "rb") as f:
            raw = f.read()

        torrent_dict = lt.bdecode(raw)
        tracker_url = f"{ANNOUNCE_TRACKER_URL}?apikey={user.apikey}"
        torrent_dict[b"announce"] = tracker_url.encode()
        torrent_dict[b"announce-list"] = [[tracker_url.encode()]]

        bencoded = lt.bencode(torrent_dict)
    except Exception as e:
        log.error(f"[GRAB] Failed to add tracker to torrent with hash '{torrent["hash_v2"]}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    await mysql.execute("UPDATE torrents SET grabs = grabs + 1 WHERE id=%s", (torrent["id"],))

    log.info(f"[GRAB] User '{user.user_label}' grabbed torrent by hash '{torrent["hash_v1"] if hash_v1 else torrent["hash_v2"]}'")

    return Response(content=bencoded, media_type="application/x-bittorrent", headers={"Content-Disposition": f'attachment; filename="{torrent_filename}"'})


@router.post("/upload")
# TODO: imdbid will need to become a required form parameter in upcoming versions
async def upload(user: User = Depends(api_key_required), category: int = Form(...), torrent_file: UploadFile = File(...), imdbid: str = Form(None),
                 tmdbid: int = Form(None)):
    user_id = user.user_id
    user_label = user.user_label

    category_id_list = [cat["id"] for cat in CATEGORIES.values()]
    if category not in category_id_list:
        raise HTTPException(status_code=400, detail="Invalid category")

    if not torrent_file.filename.endswith(".torrent"):
        raise HTTPException(status_code=400, detail="File must be torrent file")

    torrent_download_path = os.path.join(tempfile.gettempdir(), torrent_file.filename)
    with open(torrent_download_path, "wb") as f:
        f.write(await torrent_file.read())

    try:
        info = lt.torrent_info(torrent_download_path)

        torrent_name = info.name()
        normalized_torrent_name = utils.normalize_search_string(torrent_name).lower()
        file_count = len(info.files())
        size = info.total_size()
        hash_v1, hash_v2 = utils.get_torrent_hashes(torrent_download_path)

    except:
        os.unlink(torrent_download_path)
        log.error(f"[UPLOAD] Failed to process torrent file sent by '{user_label}': '{torrent_file.filename}'")
        raise HTTPException(status_code=400, detail="Invalid torrent file")

    existing = await mysql.fetch_one("SELECT id, name, added_by_user_id FROM torrents WHERE hash_v1=%s OR hash_v2=%s", (hash_v1, hash_v2))
    if existing:
        if existing["added_by_user_id"] == user_id:
            await mysql.execute("UPDATE torrents SET name = %s, normalized_name = %s, last_seen = NOW() WHERE id = %s",
                                (torrent_name, normalized_torrent_name, existing["id"]))
            log.info(f"[UPLOAD] User '{user_label}' re-uploaded torrent, renamed to '{torrent_name}'")
        else:
            log.debug(f"[UPLOAD] User '{user_label}' uploaded duplicate torrent: '{torrent_name}'")
        os.unlink(torrent_download_path)
        raise HTTPException(status_code=409, detail="Torrent with same hash exists, updated name in database")

    torrent_save_path = utils.build_torrent_path(torrent_name)
    shutil.move(torrent_download_path, torrent_save_path)

    if os.path.exists(torrent_download_path):
        os.unlink(torrent_download_path)

    season_match, episode_match = utils.extract_season_episode(torrent_name)

    await mysql.execute("""
                        INSERT INTO torrents (name, normalized_name, season, episode, imdbid, tmdbid, torrent_path, size, category, hash_v1, hash_v2, files, added_on,
                                              added_by_user_id, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW())
                        """,
                        (torrent_name, normalized_torrent_name, season_match, episode_match, imdbid, tmdbid, torrent_save_path, size, category, hash_v1, hash_v2,
                         file_count,
                         user_id))

    log.info(f"[UPLOAD] User '{user_label}' uploaded torrent '{torrent_name}'")

    return PlainTextResponse("Successfully uploaded torrent")


@router.post("/sync")
async def sync(user: User = Depends(api_key_required), request: Request = None):
    torrents: list[dict[str, int | str]] = await request.json()
    hash_v1_list = [t["hash_v1"].lower() for t in torrents if t["hash_v1"]]
    hash_v2_list = [t["hash_v2"].lower() for t in torrents if t["hash_v2"]]

    params = []
    where_clauses = []

    if hash_v1_list:
        placeholders_v1 = ", ".join(["%s"] * len(hash_v1_list))
        where_clauses.append(f"hash_v1 IN ({placeholders_v1})")
        params.extend(hash_v1_list)

    if hash_v2_list:
        placeholders_v2 = ", ".join(["%s"] * len(hash_v2_list))
        where_clauses.append(f"hash_v2 IN ({placeholders_v2})")
        params.extend(hash_v2_list)

    existing_hashes = set()
    if where_clauses:
        query = f"""
            SELECT hash_v1, hash_v2
            FROM torrents
            WHERE {" OR ".join(where_clauses)}
        """
        existing = await mysql.fetch_all(query, tuple(params))
        for row in existing:
            if row["hash_v1"]:
                existing_hashes.add(row["hash_v1"].lower())
            if row["hash_v2"]:
                existing_hashes.add(row["hash_v2"].lower())

    found = []
    missing = []
    for t in torrents:
        if t["hash_v1"] in existing_hashes or t["hash_v2"] in existing_hashes:
            found.append(t)
        else:
            missing.append(t)

    log.debug(f"[SYNC] User '{user.user_label}' synced {len(found)} existing, {len(missing)} missing (attempted {len(torrents)})")

    return JSONResponse({"found": found, "missing": missing})
