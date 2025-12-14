from fastapi import HTTPException, Request, APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from privateindexer_server.core import mysql, utils
from privateindexer_server.core.api import api_key_required
from privateindexer_server.core.config import PEER_TIMEOUT
from privateindexer_server.core.logger import log
from privateindexer_server.core.utils import User

router = APIRouter()
templates = Jinja2Templates(directory="/app/src/templates")


@router.get("/view/{torrent_id}", response_class=HTMLResponse)
async def view(torrent_id: int, request: Request, user: User = Depends(api_key_required)):
    query = f"""
        SELECT *
        FROM (
            SELECT t.*,
                   COALESCE(SUM(IF(p.left_bytes = 0, 1, 0)), 0) AS seeders,
                   COALESCE(SUM(IF(p.left_bytes > 0, 1, 0)), 0) AS leechers
            FROM torrents t
            LEFT JOIN peers p
                   ON t.id = p.torrent_id
                  AND p.last_seen > NOW() - INTERVAL %s SECOND
            WHERE t.id=%s
            GROUP BY t.id
        ) AS sub
        LIMIT 1
    """
    torrent = await mysql.fetch_one(query, (PEER_TIMEOUT, torrent_id))

    if not torrent:
        raise HTTPException(status_code=404, detail="Torrent not found")

    users_query = f"SELECT id, label FROM users WHERE public_uploads = TRUE"
    users_results = await mysql.fetch_all(users_query)
    user_map = {user["id"]: user["label"] for user in users_results}
    torrent["added_by"] = user_map.get(torrent["added_by_user_id"], "Anonymous")

    category_name = utils.get_category_name(torrent["category"])
    torrent["category_name"] = f"{category_name} ({torrent["category"]})"

    torrent["size"] = utils.format_bytes(torrent["size"])

    torrent["last_seen"] = utils.time_ago(torrent["last_seen"])

    log.info(f"[VIEW] User '{user.user_label}' viewed torrent ID {torrent_id}")

    return templates.TemplateResponse(name="view_torrent.html", context={"torrent": torrent, }, request=request)
