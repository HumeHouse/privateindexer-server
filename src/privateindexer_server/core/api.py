import datetime
import itertools
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
from privateindexer_server.core.config import CATEGORIES, ANNOUNCE_TRACKER_URL, SYNC_BATCH_SIZE
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


def latency_threshold(ms: int):
    async def set_latency_threshold(request: Request):
        request.state.latency_threshold = ms

    return set_latency_threshold


@router.get("/health")
def get_health():
    """
    Endpoint to be used by Docker for checking the readiness of the API
    """
    return PlainTextResponse("OK")


@router.get("/analytics", dependencies=[Depends(latency_threshold(1000))])
async def get_analytics(user: User = Depends(api_key_required)):
    log.debug(f"[ANALYTICS] User '{user.user_label}' requested analytics")
    try:
        redis_connection = redis.get_connection()

        requests = int((await redis_connection.get("stats:requests")) or 0)
        bytes_sent = int((await redis_connection.get("stats:bytes_sent")) or 0)
        bytes_received = int((await redis_connection.get("stats:bytes_received")) or 0)
        unique_visitors = await redis_connection.scard("stats:unique_ips")

        times_raw = await redis_connection.lrange("stats:request_times", -1000, -1)
        times = [float(t) for t in times_raw] if times_raw else []

        request_time_avg = (sum(times) / len(times)) / 1000 if times else 0.0
        request_time_min = min(times) / 1000 if times else 0.0
        request_time_max = max(times) / 1000 if times else 0.0

        # get all peer keys at once
        peer_keys = []
        cursor = 0
        while True:
            cursor, keys = await redis_connection.scan(cursor=cursor, match="peer:*:*", count=1000)
            peer_keys.extend(keys)
            if cursor == 0:
                break

        # fetch all peer hashes in one go using a pipeline
        pipe = redis_connection.pipeline()
        for key in peer_keys:
            await pipe.hgetall(key)
        all_peers_data = await pipe.execute()  # list of dicts

        # aggregate by torrent
        torrents = {}
        for key, pdata in zip(peer_keys, all_peers_data):
            if not pdata:
                continue

            # parse torrent_id from key "peer:{torrent_id}:{peer_id}"
            _, torrent_id, _ = key.split(":")
            torrent_id = int(torrent_id)

            if torrent_id not in torrents:
                torrents[torrent_id] = {"seeders": 0, "leechers": 0}

            left = int(pdata.get("left", 1))
            if left == 0:
                torrents[torrent_id]["seeders"] += 1
            else:
                torrents[torrent_id]["leechers"] += 1

        # now you have seeders/leechers per torrent without per-peer roundtrips
        total_peers = sum(v["seeders"] + v["leechers"] for v in torrents.values())
        seeding_torrents = sum(1 for v in torrents.values() if v["seeders"])
        leeching_torrents = sum(1 for v in torrents.values() if v["leechers"])

    except Exception as e:
        log.error(f"[ANALYTICS] Failed to get analytics from Redis: {e}")
        return JSONResponse({})

    data_transfer = await mysql.fetch_one("SELECT SUM(downloaded) AS total_downloaded, SUM(uploaded) AS total_uploaded FROM users")
    total_downloaded = int(data_transfer["total_downloaded"] or 0)
    total_uploaded = int(data_transfer["total_uploaded"] or 0)

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
            for torrent_result in results:
                torrent_id = torrent_result["id"]

                try:
                    seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
                except Exception as e:
                    seeders = leechers = 0
                    log.error(f"[TORZNAB] Failed to fetch seeders/leechers from Redis: {e}")

                torrent_url_with_key = f"https://indexer.humehouse.com/grab?infohash={torrent_result['hash_v2']}&apikey={user.apikey}"
                torrent_link = f"https://indexer.humehouse.com/view/{torrent_result["id"]}?apikey={user.apikey}"
                items.append(f"""
                    <item>
                        <title>{escape(torrent_result["name"])}</title>
                        <guid isPermaLink="false">humehouse-{torrent_result['hash_v2']}</guid>
                        <comments>{escape(torrent_link)}</comments>
                        <enclosure url="{escape(torrent_url_with_key)}" length="{torrent_result["size"]}" type="application/x-bittorrent"/>
                        <size>{torrent_result["size"]}</size>
                        <pubDate>{torrent_result["added_on"].strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate>
                        <category>{torrent_result["category"]}</category>
                        <torznab:attr name="category" value="{torrent_result["category"]}" />
                        <torznab:attr name="files" value="{torrent_result['files']}"/>
                        <torznab:attr name="seeders" value="{seeders}"/>
                        <torznab:attr name="leechers" value="{leechers}"/>
                        <torznab:attr name="grabs" value="{torrent_result['grabs']}"/>
                        <torznab:attr name="infohash" value="{torrent_result['hash_v2']}"/>
                        {f"<torznab:attr name=\"imdbid\" value=\"{torrent_result["imdbid"]}\"/>" if torrent_result.get("imdbid") else ""}
                        {f"<torznab:attr name=\"tmdbid\" value=\"{torrent_result["tmdbid"]}\"/>" if torrent_result.get("tmdbid") else ""}
                        {f"<torznab:attr name=\"tvdbid\" value=\"{torrent_result["tvdbid"]}\"/>" if torrent_result.get("tvdbid") else ""}
                        {f"<torznab:attr name=\"season\" value=\"{torrent_result["season"]}\"/>" if torrent_result.get("season") else ""}
                        {f"<torznab:attr name=\"episode\" value=\"{torrent_result["episode"]}\"/>" if torrent_result.get("episode") else ""}
                        {f"<torznab:attr name=\"artist\" value=\"{torrent_result["artist"]}\"/>" if torrent_result.get("artist") else ""}
                        {f"<torznab:attr name=\"album\" value=\"{torrent_result["album"]}\"/>" if torrent_result.get("album") else ""}
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
            normalized_q = f"%{utils.clean_text_filter(q)}%"
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
                normalized_artist = utils.clean_text_filter(artist)
                where_clauses.append("t.artist = %s")
                where_params.append(normalized_artist)
            if album is not None:
                normalized_album = utils.clean_text_filter(album)
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
            FROM torrents t
            WHERE {where_sql}
            ORDER BY added_on DESC
            LIMIT %s OFFSET %s
        """

        query_params = tuple(where_params) + (int(limit), int(offset))
        results = await mysql.fetch_all(query, query_params)
        total_matches = results[0]["total_matches"] if results else 0

        delta = datetime.datetime.now() - before
        query_duration = f"{round(delta.total_seconds() * 1000)} ms"
        search_params = {"cat": cat, "season": season, "ep": ep, "imdbid": imdbid, "tmdbid": tmdbid, "tvdbid": tvdbid, "artist": artist, "album": album}
        search_params = ",".join(f"{k}={v}" for k, v in search_params.items() if v is not None)
        log.info(f"[TORZNAB] User '{user.user_label}' searched{f" '{q}'" if q else ""} with params {search_params} ({query_duration}): "
                 f"returned {len(results)} results, found {total_matches} total")

        items = []
        for torrent_result in results:
            torrent_id = torrent_result["id"]

            try:
                seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
            except Exception as e:
                seeders = leechers = 0
                log.error(f"[TORZNAB] Failed to fetch seeders/leechers from Redis: {e}")

            torrent_url_with_key = f"https://indexer.humehouse.com/grab?infohash={torrent_result['hash_v2']}&apikey={user.apikey}"
            torrent_link = f"https://indexer.humehouse.com/view/{torrent_result["id"]}?apikey={user.apikey}"
            item = f"""
            <item>
                <title>{escape(torrent_result["name"])}</title>
                <guid isPermaLink="false">humehouse-{torrent_result['hash_v2']}</guid>
                <link>{escape(torrent_url_with_key)}</link>
                <comments>{escape(torrent_link)}</comments>
                <enclosure url="{escape(torrent_url_with_key)}" length="{torrent_result["size"]}" type="application/x-bittorrent"/>
                <size>{torrent_result["size"]}</size>
                <pubDate>{torrent_result["added_on"].strftime("%a, %d %b %Y %H:%M:%S GMT")}</pubDate>
                <category>{torrent_result["category"]}</category>
                <torznab:attr name="category" value="{torrent_result["category"]}" />
                <torznab:attr name="files" value="{torrent_result['files']}"/>
                <torznab:attr name="seeders" value="{seeders}"/>
                <torznab:attr name="leechers" value="{leechers}"/>
                <torznab:attr name="peers" value="{seeders + leechers}"/>
                <torznab:attr name="grabs" value="{torrent_result['grabs']}"/>
                <torznab:attr name="infohash" value="{torrent_result['hash_v2']}"/>
                {f"<torznab:attr name=\"imdbid\" value=\"{torrent_result["imdbid"]}\"/>" if torrent_result.get("imdbid") else ""}
                {f"<torznab:attr name=\"tmdbid\" value=\"{torrent_result["tmdbid"]}\"/>" if torrent_result.get("tmdbid") else ""}
                {f"<torznab:attr name=\"tvdbid\" value=\"{torrent_result["tvdbid"]}\"/>" if torrent_result.get("tvdbid") else ""}
                {f"<torznab:attr name=\"season\" value=\"{torrent_result["season"]}\"/>" if torrent_result.get("season") else ""}
                {f"<torznab:attr name=\"episode\" value=\"{torrent_result["episode"]}\"/>" if torrent_result.get("episode") else ""}
                {f"<torznab:attr name=\"artist\" value=\"{torrent_result["artist"]}\"/>" if torrent_result.get("artist") else ""}
                {f"<torznab:attr name=\"album\" value=\"{torrent_result["album"]}\"/>" if torrent_result.get("album") else ""}
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
        normalized_torrent_name = utils.clean_text_filter(torrent_name)
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
        artist = utils.clean_text_filter(artist)

    if album:
        album = utils.clean_text_filter(album)

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


@router.post("/sync", dependencies=[Depends(latency_threshold(5000))])
async def sync(user: User = Depends(api_key_required), request: Request = None):
    torrents: list[dict[str, int | str]] = await request.json()

    rows = []
    for t in torrents:
        infohash = t.get("infohash")
        torrent_name = t.get("name")
        normalized_torrent_name = utils.clean_text_filter(torrent_name) if torrent_name else None

        if infohash:
            rows.append((t["id"], infohash, torrent_name, normalized_torrent_name))

    if not rows:
        return JSONResponse({"missing_ids": [t["id"] for t in torrents]})

    missing_ids: list[int] = []

    for batch in itertools.batched(rows, SYNC_BATCH_SIZE):
        selects = []
        params = []

        for torrent_id, infohash, torrent_name, normalized_torrent_name in batch:
            selects.append("SELECT %s AS id, %s AS infohash, %s AS name, %s AS normalized_name")
            params.extend([torrent_id, infohash, torrent_name, normalized_torrent_name])

        union_sql = " UNION ALL ".join(selects)

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

        update_query = f"""
                    UPDATE torrents t
                    JOIN (
                        {union_sql}
                    ) AS c
                      ON c.infohash = t.hash_v2
                    SET t.name = c.name, t.normalized_name = c.normalized_name
                    WHERE
                        c.name IS NOT NULL AND c.normalized_name IS NOT NULL
                        AND (t.name != c.name OR t.normalized_name != c.normalized_name)
                """

        await mysql.execute(update_query, params)

    log.debug(f"[SYNC] User '{user.user_label}' performed sync: {len(missing_ids)} missing (sent {len(torrents)})")

    return JSONResponse({"missing_ids": missing_ids})
