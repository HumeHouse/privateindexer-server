import datetime
import os
import re
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
async def current_user(user: User = Depends(api_key_required), request: Request = None, v: str = Query(...), announce_ip: str = Query(None), port: int = Query(None),
                       public_uploads: bool = Query(...)):
    announce_ip = announce_ip or utils.get_client_ip(request)
    port = port or 6881

    reachable = False
    try:
        with socket.create_connection((announce_ip, port), timeout=5):
            reachable = True
            log.info(f"[USER] User '{user.user_label}' ({announce_ip}:{port}) connected with PrivateIndexer client v{v}")
    except (socket.timeout, ConnectionRefusedError, OSError):
        log.warning(f"[USER] User '{user.user_label}' ({announce_ip}:{port} - UNREACHABLE) connected with PrivateIndexer client v{v}")
        pass

    await mysql.execute("UPDATE users SET client_version = %s, last_ip = %s, last_seen=NOW(), reachable = %s, public_uploads = %s WHERE id = %s",
                        (v, f"{announce_ip}:{port}", reachable, public_uploads, user.user_id))

    user_data = {"user_label": user.user_label, "announce_ip": announce_ip, "is_reachable": reachable, }
    return JSONResponse(user_data)


@router.get("/user/stats")
async def get_user_stats(user: User = Depends(api_key_required)):
    log.debug(f"[USER] User '{user.user_label}' requested statistics")

    user_id = user.user_id

    stats_query = "SELECT torrents_uploaded, grabs, downloaded, uploaded, seeding, leeching FROM users WHERE id = %s"
    stats = await mysql.fetch_one(stats_query, (user_id,))

    torrents_uploaded = int(stats["torrents_uploaded"] or 0)
    grabs = int(stats["grabs"] or 0)
    downloaded = stats["downloaded"] or 0
    uploaded = stats["uploaded"] or 0
    seeding = int(stats["seeding"] or 0)
    leeching = int(stats["leeching"] or 0)

    if downloaded > 0:
        server_ratio = uploaded / downloaded
    elif uploaded > 0:
        server_ratio = 8640000
    else:
        server_ratio = 0.0

    return JSONResponse(
        {"user": user.user_label, "torrents_added_total": torrents_uploaded, "currently_seeding": seeding, "currently_leeching": leeching, "grabs_total": grabs,
         "total_download": downloaded, "total_upload": uploaded, "server_ratio": server_ratio, })


@router.get("/api")
async def torznab_api(user: User = Depends(api_key_required), t: str = Query(...), q: str = Query(""), cat: str = Query(None), season: int = Query(None),
                      ep: int = Query(None), imdbid: int = Query(None), tmdbid: int = Query(None), tvdbid: int = Query(None), artist: str = Query(None),
                      album: str = Query(None), limit: int = Query(100), offset: int = Query(0), include_my_uploads: bool = Query(False)):
    if t == "caps":
        log.debug(f"[TORZNAB] User '{user.user_label}' sent capability request")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <caps>
            <server version="1.0" title="HumeHouse PrivateIndexer Server"/>
            <limits default="100" max="1000"/>
            <categories>
            {''.join([f'<category id="{c["id"]}" name="{c["name"]}"/>' for c in CATEGORIES])}
            </categories>
            <searching>
                <search available="yes" supportedParams="q"/>
                <tv-search available="yes" supportedParams="q,season,ep,imdbid,tmdbid,tvdbid"/>
                <movie-search available="yes" supportedParams="q,imdbid,tmdbid"/>
                <music-search available="yes" supportedParams="q,artist,album"/>
                <book-search available="no"/>
            </searching>
        </caps>"""
        return Response(content=xml, media_type="application/xml")

    elif t in ["search", "tvsearch", "movie", "music"]:
        limit = min(int(limit), 1000)
        before = datetime.datetime.now()

        where_clauses = []
        where_params = []

        if not include_my_uploads:
            where_clauses.append(f"t.added_by_user_id != %s")
            where_params.append(user.user_id)

        # if no query is specified in a regular search, assume an RSS query is being made
        if t == "search" and (not q or q.strip() == ""):

            if cat is not None:
                cats = [int(c) for c in cat.split(",")]
                where_clauses.append(f"t.category IN ({",".join(["%s"] * len(cats))})")
                where_params.extend(cats)

            where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

            # perform a lightweight scan of just most recent torrents
            rss_query = f"SELECT * FROM torrents t WHERE {where_sql} ORDER BY added_on DESC LIMIT %s OFFSET %s"
            query_params = tuple(where_params) + (int(limit), int(offset))
            results = await mysql.fetch_all(rss_query, query_params)

            items = []
            for t_entry in results:
                torrent_url_with_key = f"https://indexer.humehouse.com/grab?infohash={t_entry['hash_v2']}&apikey={user.apikey}"
                torrent_link = f"https://indexer.humehouse.com/view/{t_entry["id"]}?apikey={user.apikey}"
                items.append(f"""
                    <item>
                        <title>{escape(t_entry["name"])}</title>
                        <guid isPermaLink="false">humehouse-{t_entry['hash_v2']}</guid>
                        <comments>{escape(torrent_link)}</comments>
                        <enclosure url="{escape(torrent_url_with_key)}" length="{t_entry["size"]}" type="application/x-bittorrent"/>
                        <size>{t_entry["size"]}</size>
                        <pubDate>{t_entry["added_on"].strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate>
                        <category>{t_entry["category"]}</category>
                        <torznab:attr name="category" value="{t_entry["category"]}" />
                        <torznab:attr name="files" value="{t_entry['files']}"/>
                        <torznab:attr name="grabs" value="{t_entry['grabs']}"/>
                        <torznab:attr name="infohash" value="{t_entry['hash_v2']}"/>
                        {f"<torznab:attr name=\"imdbid\" value=\"{t_entry["imdbid"]}\"/>" if t_entry.get("imdbid") else ""}
                        {f"<torznab:attr name=\"tmdbid\" value=\"{t_entry["tmdbid"]}\"/>" if t_entry.get("tmdbid") else ""}
                        {f"<torznab:attr name=\"tvdbid\" value=\"{t_entry["tvdbid"]}\"/>" if t_entry.get("tvdbid") else ""}
                        {f"<torznab:attr name=\"season\" value=\"{t_entry["season"]}\"/>" if t_entry.get("season") else ""}
                        {f"<torznab:attr name=\"episode\" value=\"{t_entry["episode"]}\"/>" if t_entry.get("episode") else ""}
                        {f"<torznab:attr name=\"artist\" value=\"{t_entry["artist"]}\"/>" if t_entry.get("artist") else ""}
                        {f"<torznab:attr name=\"album\" value=\"{t_entry["album"]}\"/>" if t_entry.get("album") else ""}
                    </item>
                """)

            xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
                    <channel>
                        <title>HumeHouse PrivateIndexer</title>
                        <description>For friends of David</description>
                        {"".join(items)}
                    </channel>
                </rss>
            """

            delta = datetime.datetime.now() - before
            query_duration = f"{round(delta.total_seconds() * 1000)} ms"

            log.debug(f"[TORZNAB] User '{user.user_label}' performed RSS feed query in category {cat} ({query_duration}): returned {len(results)} results")

            return Response(content=xml, media_type="application/xml")

        if q is not None:
            normalized_q = f"%{utils.normalize_search_string(q)}%"
            where_clauses.append("t.normalized_name LIKE %s")
            where_params.append(normalized_q)

        if cat is not None:
            cats = [int(c) for c in cat.split(",")]
            where_clauses.append(f"t.category IN ({",".join(["%s"] * len(cats))})")
            where_params.extend(cats)

        if t == "tvsearch":
            if season is not None:
                where_clauses.append("t.season = %s")
                where_params.append(int(season))

                if ep is not None:
                    where_clauses.append("t.episode = %s")
                    where_params.append(int(ep))
                else:
                    where_clauses.append("t.episode IS NULL")
            where_clauses.append("t.artist IS NULL")
            where_clauses.append("t.album IS NULL")
        elif t == "movie":
            where_clauses.append("t.season IS NULL")
            where_clauses.append("t.episode IS NULL")
            where_clauses.append("t.artist IS NULL")
            where_clauses.append("t.album IS NULL")
        elif t == "music":
            where_clauses.append("t.season IS NULL")
            where_clauses.append("t.episode IS NULL")
            if artist is not None:
                normalized_artist = utils.normalize_search_string(artist)
                where_clauses.append("t.artist = %s")
                where_params.append(normalized_artist)
            if album is not None:
                normalized_album = utils.normalize_search_string(album)
                where_clauses.append("t.album = %s")
                where_params.append(normalized_album)

        or_clauses = []
        or_params = []

        if imdbid:
            or_clauses.append("t.imdbid = %s")
            or_params.append(imdbid)

        if tmdbid:
            or_clauses.append("t.tmdbid = %s")
            or_params.append(tmdbid)

        if tvdbid:
            or_clauses.append("t.tvdbid = %s")
            or_params.append(tvdbid)

        if or_clauses:
            where_clauses.append("(" + " OR ".join(or_clauses) + ")")
            where_params.extend(or_params)

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

        query_params = (int(PEER_TIMEOUT),) + tuple(where_params) + (int(limit), int(offset))
        results = await mysql.fetch_all(query, query_params)
        total_matches = results[0]["total_matches"] if results else 0

        delta = datetime.datetime.now() - before
        query_duration = f"{round(delta.total_seconds() * 1000)} ms"
        search_params = {"cat": cat, "season": season, "ep": ep, "imdbid": imdbid, "tmdbid": tmdbid, "tvdbid": tvdbid, "artist": artist, "album": album}
        search_params = ",".join(f"{k}={v}" for k, v in search_params.items() if v is not None)
        log.info(f"[TORZNAB] User '{user.user_label}' searched{f" '{q}'" if q else ""} with params {search_params} ({query_duration}): "
                 f"returned {len(results)} results, found {total_matches} total")

        items = []
        for t_entry in results:
            seeders = t_entry["seeders"]
            leechers = t_entry["leechers"]

            torrent_url_with_key = f"https://indexer.humehouse.com/grab?infohash={t_entry['hash_v2']}&apikey={user.apikey}"
            torrent_link = f"https://indexer.humehouse.com/view/{t_entry["id"]}?apikey={user.apikey}"
            item = f"""
            <item>
                <title>{escape(t_entry["name"])}</title>
                <guid isPermaLink="false">humehouse-{t_entry['hash_v2']}</guid>
                <link>{escape(torrent_url_with_key)}</link>
                <comments>{escape(torrent_link)}</comments>
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
                {f"<torznab:attr name=\"tvdbid\" value=\"{t_entry["tvdbid"]}\"/>" if t_entry.get("tvdbid") else ""}
                {f"<torznab:attr name=\"season\" value=\"{t_entry["season"]}\"/>" if t_entry.get("season") else ""}
                {f"<torznab:attr name=\"episode\" value=\"{t_entry["episode"]}\"/>" if t_entry.get("episode") else ""}
                {f"<torznab:attr name=\"artist\" value=\"{t_entry["artist"]}\"/>" if t_entry.get("artist") else ""}
                {f"<torznab:attr name=\"album\" value=\"{t_entry["album"]}\"/>" if t_entry.get("album") else ""}
            </item>
            """
            items.append(item)

        xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
            <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
                <channel>
                    <title>HumeHouse PrivateIndexer</title>
                    <description>For friends of David</description>
                    <link>https://indexer.humehouse.com/api</link>
                    <torznab:response offset="{offset}" total="{total_matches}"/>
                    {"".join(items)}
                </channel>
            </rss>
        """
        return Response(content=xml, media_type="application/xml")

    else:
        log.warning(f"[TORZNAB] User '{user.user_label}' attemped an invalid search type: {t}")
        raise HTTPException(status_code=400, detail="Unsupported request type")


@router.get("/grab")
async def grab(user: User = Depends(api_key_required), infohash: str = Query(...), nograb: bool = Query(False)):
    torrent = await mysql.fetch_one("SELECT id, torrent_path FROM torrents WHERE hash_v2 = %s LIMIT 1", (infohash,))
    if not torrent:
        log.debug(f"[GRAB] User '{user.user_label}' tried to grab invalid torrent with hash '{infohash}'")
        raise HTTPException(status_code=404, detail="Torrent not found")

    torrent_path = torrent["torrent_path"]
    if not os.path.exists(torrent_path):
        log.error(f"[GRAB] Torrent file missing for hash {infohash}")
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
        log.error(f"[GRAB] Failed to add tracker to torrent with hash '{infohash}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

    # only increment the grab counter and log it if query param was not set
    if not nograb:
        await mysql.execute("UPDATE torrents SET grabs = grabs + 1 WHERE id=%s", (torrent["id"],))

        log.info(f"[GRAB] User '{user.user_label}' grabbed torrent by hash '{infohash}'")

    return Response(content=bencoded, media_type="application/x-bittorrent", headers={"Content-Disposition": f'attachment; filename="{torrent_filename}"'})


@router.post("/upload")
async def upload(user: User = Depends(api_key_required), category: int = Form(...), torrent_file: UploadFile = File(...), torrent_name: str = Form(...),
                 imdbid: str = Form(None), tmdbid: int = Form(None), tvdbid: int = Form(None), artist: str = Form(None), album: str = Form(None)):
    user_id = user.user_id
    user_label = user.user_label

    category_id_list = [cat["id"] for cat in CATEGORIES]
    if category not in category_id_list:
        raise HTTPException(status_code=400, detail="Invalid category")

    if not torrent_file.filename.endswith(".torrent"):
        raise HTTPException(status_code=400, detail="File must be torrent file")

    torrent_download_path = os.path.join(tempfile.gettempdir(), torrent_file.filename)
    with open(torrent_download_path, "wb") as f:
        f.write(await torrent_file.read())

    try:
        info = lt.torrent_info(torrent_download_path)
        normalized_torrent_name = utils.normalize_search_string(torrent_name)
        file_count = len(info.files())
        size = info.total_size()
        hash_v1, hash_v2 = utils.get_torrent_hashes(torrent_download_path)

        hash_v2_truncated = hash_v2[:40]

        season_match, episode_match = utils.extract_season_episode(torrent_name)
    except Exception as e:
        os.unlink(torrent_download_path)
        log.error(f"[UPLOAD] Failed to process torrent file sent by '{user_label}': '{torrent_file.filename}': {e}")
        raise HTTPException(status_code=400, detail="Invalid torrent file")

    if imdbid:
        imdbid = int(re.sub(r"\D", "", imdbid))

    if artist:
        artist = utils.normalize_search_string(artist)

    if album:
        album = utils.normalize_search_string(album)

    existing = await mysql.fetch_one("SELECT id, name, added_by_user_id FROM torrents WHERE hash_v1=%s OR hash_v2=%s", (hash_v1, hash_v2))
    if existing:
        if existing["added_by_user_id"] == user_id:
            await mysql.execute(
                "UPDATE torrents SET name = %s, normalized_name = %s, hash_v1 = %s, hash_v2 = %s, hash_v2_trunc = %s, season = %s, episode = %s, imdbid = %s, tmdbid = %s, tvdbid = %s, artist = %s, album = %s, last_seen = NOW() WHERE id = %s",
                (torrent_name, normalized_torrent_name, hash_v1, hash_v2, hash_v2_truncated, season_match, episode_match, imdbid, tmdbid, tvdbid, artist, album,
                 existing["id"]))
            log.info(f"[UPLOAD] User '{user_label}' re-uploaded torrent, renamed to '{torrent_name}'")
        else:
            log.debug(f"[UPLOAD] User '{user_label}' uploaded duplicate torrent: '{torrent_name}'")
        os.unlink(torrent_download_path)
        raise HTTPException(status_code=409, detail="Torrent with same hash exists, updated name in database")

    torrent_save_path = utils.build_torrent_path(torrent_name)
    shutil.move(torrent_download_path, torrent_save_path)

    if os.path.exists(torrent_download_path):
        os.unlink(torrent_download_path)

    await mysql.execute("""
                        INSERT INTO torrents (name, normalized_name, season, episode, imdbid, tmdbid, tvdbid, artist, album, torrent_path, size, category, hash_v1,
                                              hash_v2, hash_v2_trunc, files, added_on, added_by_user_id, last_seen)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, NOW())
                        """,
                        (torrent_name, normalized_torrent_name, season_match, episode_match, imdbid, tmdbid, tvdbid, artist, album, torrent_save_path, size, category,
                         hash_v1, hash_v2, hash_v2_truncated, file_count, user_id))

    log.info(f"[UPLOAD] User '{user_label}' uploaded torrent '{torrent_name}'")

    return PlainTextResponse("Successfully uploaded torrent")


@router.post("/sync")
async def sync(user: User = Depends(api_key_required), request: Request = None):
    torrents: list[dict[str, int | str]] = await request.json()

    client_hashes = [torrent["infohash"] for torrent in torrents if torrent.get("infohash")]

    if not client_hashes:
        missing_ids = [torrent["id"] for torrent in torrents]
        return JSONResponse({"missing_ids": missing_ids})

    params = client_hashes
    placeholders = ", ".join(["%s"] * len(params))

    params.append(user.user_id)

    query = f"SELECT hash_v2 FROM torrents WHERE hash_v2 IN ({placeholders}) AND (hash_v1 IS NOT NULL OR added_by_user_id != %s)"
    rows = await mysql.fetch_all(query, params)

    existing_hashes = {row["hash_v2"] for row in rows}

    missing_ids = [torrent["id"] for torrent in torrents if torrent["infohash"] not in existing_hashes]

    log.debug(f"[SYNC] User '{user.user_label}' synced {len(existing_hashes)} existing, {len(missing_ids)} missing (attempted {len(torrents)})")

    return JSONResponse({"missing_ids": missing_ids})
