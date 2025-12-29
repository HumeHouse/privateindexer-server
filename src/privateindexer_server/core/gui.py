from fastapi import HTTPException, Request, APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from privateindexer_server.core import mysql, utils
from privateindexer_server.core.api import api_key_required
from privateindexer_server.core.logger import log
from privateindexer_server.core.utils import User

router = APIRouter()
templates = Jinja2Templates(directory="/app/src/templates")


@router.get("/view/{torrent_id}", response_class=HTMLResponse)
async def view(torrent_id: int, request: Request, user: User = Depends(api_key_required)):
    torrent = await mysql.fetch_one("SELECT * FROM torrents WHERE id=%s", (torrent_id,))

    try:
        seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
    except Exception as e:
        seeders = leechers = 0
        log.error(f"[VIEW] Failed to fetch seeders/leechers from Redis: {e}")

    torrent["seeders"] = seeders
    torrent["leechers"] = leechers

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
