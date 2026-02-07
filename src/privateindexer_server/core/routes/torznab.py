import datetime
from xml.sax.saxutils import escape

from fastapi import Depends, Query, HTTPException, APIRouter
from fastapi.responses import Response

from privateindexer_server.core import jwt_helper, mysql, utils
from privateindexer_server.core.config import SITE_NAME, CATEGORIES, EXTERNAL_SERVER_URL
from privateindexer_server.core.logger import log
from privateindexer_server.core.routes.route_helper import api_key_required
from privateindexer_server.core.user_helper import User

router = APIRouter()


@router.get("/api")
async def torznab_api(user: User = Depends(api_key_required), t: str = Query(...), q: str = Query(""), cat: str = Query(None), season: int = Query(None),
                      ep: int = Query(None), imdbid: int = Query(None), tmdbid: int = Query(None), tvdbid: int = Query(None), artist: str = Query(None),
                      album: str = Query(None), limit: int = Query(100), offset: int = Query(0), include_my_uploads: bool = Query(False)):
    """
    Called by apps like Radarr/Sonarr/Lidarr to look for torrents which match a set of search parameters or perform RSS queries for the latest indexer uploads
    """
    # the client is sending us a capabilities probe request to check what query parameters the server is capable of providing to the clients
    if t == "caps":
        log.debug(f"[TORZNAB] User '{user.user_label}' sent capability request")
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
        <caps>
            <server version="1.0" title="{SITE_NAME}"/>
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

    # the client is performing a torrent query
    elif t in ["search", "tvsearch", "movie", "music"]:
        before = datetime.datetime.now()

        # max out the limit to 1000 results
        limit = min(int(limit), 1000)

        # start a list of where clauses and parameters for the SQL query
        where_clauses = []
        where_params = []

        if not include_my_uploads:
            where_clauses.append(f"t.added_by_user_id != %s")
            where_params.append(user.user_id)

        # generate access token to be inserted into returned torrent view and grab URLs
        view_access_token = jwt_helper.create_access_token(user.user_id, "view")
        grab_access_token = jwt_helper.create_access_token(user.user_id, "grab")

        # if no query is specified in a regular search, assume an RSS query is being made
        if t == "search" and (not q or q.strip() == ""):

            # add category where clause
            if cat is not None:
                cats = [int(c) for c in cat.split(",")]
                where_clauses.append(f"t.category IN ({",".join(["%s"] * len(cats))})")
                where_params.extend(cats)

            # add a default TRUE if no where clauses have been added
            where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

            # perform a lightweight scan of just most recent torrents
            rss_query = f"SELECT * FROM torrents t WHERE {where_sql} ORDER BY added_on DESC LIMIT %s OFFSET %s"
            query_params = tuple(where_params) + (int(limit), int(offset))
            results = await mysql.fetch_all(rss_query, query_params)

            # assemble the full RSS query response
            items = []
            for torrent_result in results:
                torrent_id = torrent_result["id"]

                # attempt to fetch the seed and leech count from Redis to enrich the RSS response
                try:
                    seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
                except Exception as e:
                    seeders = leechers = 0
                    log.error(f"[TORZNAB] Failed to fetch seeders/leechers from Redis: {e}")

                # feed the client URLs with the torrent hash and an access token
                grab_link = f"{EXTERNAL_SERVER_URL}/grab?infohash={torrent_result['hash_v2']}&at={grab_access_token}"
                view_link = f"{EXTERNAL_SERVER_URL}/view/{torrent_result["id"]}?at={view_access_token}"
                items.append(f"""
                    <item>
                        <title>{escape(torrent_result["name"])}</title>
                        <guid isPermaLink="false">humehouse-{torrent_result['hash_v2']}</guid>
                        <link>{escape(grab_link)}</link>
                        <comments>{escape(view_link)}</comments>
                        <enclosure url="{escape(grab_link)}" length="{torrent_result["size"]}" type="application/x-bittorrent"/>
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

            # build the final XML object
            xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
                <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
                    <channel>
                        <title>{SITE_NAME}</title>
                        {"".join(items)}
                    </channel>
                </rss>
            """

            delta = datetime.datetime.now() - before
            query_duration = f"{round(delta.total_seconds() * 1000)} ms"

            log.debug(f"[TORZNAB] User '{user.user_label}' performed RSS feed query in category {cat} ({query_duration}): returned {len(results)} results")

            return Response(content=xml, media_type="application/xml")

        # add the plain text query where clause
        if q is not None:
            # here we try to normalize the query by transliterating the unicode
            normalized_q = f"%{utils.clean_text_filter(q)}%"
            where_clauses.append("t.normalized_name LIKE %s")
            where_params.append(normalized_q)

        # add category where clause
        if cat is not None:
            cats = [int(c) for c in cat.split(",")]
            where_clauses.append(f"t.category IN ({",".join(["%s"] * len(cats))})")
            where_params.extend(cats)

        # add TV-related where clauses
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

        # add movie-related where clauses
        elif t == "movie":
            where_clauses.append("t.season IS NULL")
            where_clauses.append("t.episode IS NULL")
            where_clauses.append("t.artist IS NULL")
            where_clauses.append("t.album IS NULL")

        # add music-related where clauses
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

        # add in extra parameters with OR operator
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

        # add a default TRUE if no where clauses have been added
        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # assemble the final query
        query = f"""
            SELECT *, COUNT(*) OVER() AS total_matches
            FROM torrents t
            WHERE {where_sql}
            ORDER BY added_on DESC
            LIMIT %s OFFSET %s
        """

        # time and execute the query
        query_params = tuple(where_params) + (int(limit), int(offset))
        results = await mysql.fetch_all(query, query_params)
        total_matches = results[0]["total_matches"] if results else 0

        delta = datetime.datetime.now() - before
        query_duration = f"{round(delta.total_seconds() * 1000)} ms"

        # reassemble the full request string for logging
        search_params = {"cat": cat, "season": season, "ep": ep, "imdbid": imdbid, "tmdbid": tmdbid, "tvdbid": tvdbid, "artist": artist, "album": album}
        search_params = ",".join(f"{k}={v}" for k, v in search_params.items() if v is not None)
        log.info(f"[TORZNAB] User '{user.user_label}' searched{f" '{q}'" if q else ""} with params {search_params} ({query_duration}): "
                 f"returned {len(results)} results, found {total_matches} total")

        # assemble the full query response
        items = []
        for torrent_result in results:
            torrent_id = torrent_result["id"]

            # attempt to fetch the seed and leech count from Redis to enrich the query response
            try:
                seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
            except Exception as e:
                seeders = leechers = 0
                log.error(f"[TORZNAB] Failed to fetch seeders/leechers from Redis: {e}")

            # feed the client URLs with the torrent hash and an access token
            grab_link = f"{EXTERNAL_SERVER_URL}/grab?infohash={torrent_result['hash_v2']}&at={grab_access_token}"
            view_link = f"{EXTERNAL_SERVER_URL}/view/{torrent_result["id"]}?at={view_access_token}"
            item = f"""
            <item>
                <title>{escape(torrent_result["name"])}</title>
                <guid isPermaLink="false">humehouse-{torrent_result['hash_v2']}</guid>
                <link>{escape(grab_link)}</link>
                <comments>{escape(view_link)}</comments>
                <enclosure url="{escape(grab_link)}" length="{torrent_result["size"]}" type="application/x-bittorrent"/>
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

        # build the final XML object
        xml = f"""<?xml version="1.0" encoding="UTF-8" ?>
            <rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
                <channel>
                    <title>{SITE_NAME}</title>
                    <link>{EXTERNAL_SERVER_URL}/api</link>
                    <torznab:response offset="{offset}" total="{total_matches}"/>
                    {"".join(items)}
                </channel>
            </rss>
        """
        return Response(content=xml, media_type="application/xml")

    # the user is performing an unknown or unsupported query type
    else:
        log.warning(f"[TORZNAB] User '{user.user_label}' attemped an invalid search type: {t}")
        raise HTTPException(status_code=400, detail="Unsupported request type")
