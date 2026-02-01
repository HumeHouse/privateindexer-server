from fastapi import HTTPException, Request, APIRouter, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from privateindexer_server.core import mysql, utils
from privateindexer_server.core.api import access_token_required
from privateindexer_server.core.logger import log
from privateindexer_server.core.user_helper import User

router = APIRouter()
templates = Jinja2Templates(directory="/app/src/templates")


@router.get("/view/{torrent_id}", response_class=HTMLResponse)
async def view(torrent_id: int, request: Request, user: User = Depends(access_token_required)):
    """
    Endpoint called by users to view data about a torrent in a browser
    Serves a Jinja HTML template
    """
    # fetch the torrent data from database
    torrent = await mysql.fetch_one("SELECT * FROM torrents WHERE id=%s", (torrent_id,))

    # ensure torrent ID exists in database
    if not torrent:
        raise HTTPException(status_code=404, detail="Torrent not found")

    # try to fetch torrent seeders/leechers from Redis database
    try:
        seeders, leechers = await utils.get_seeders_and_leechers(torrent_id)
    except Exception as e:
        seeders = leechers = 0
        log.error(f"[VIEW] Failed to fetch seeders/leechers from Redis: {e}")

    torrent["seeders"] = seeders
    torrent["leechers"] = leechers

    # get a list of users who wish to label their uploads from the database
    users_query = f"SELECT id, label FROM users WHERE public_uploads = TRUE"
    users_results = await mysql.fetch_all(users_query)

    # try to match the uploaer ID with a user label in the database results
    user_map = {user["id"]: user["label"] for user in users_results}
    torrent["added_by"] = user_map.get(torrent["added_by_user_id"], "Anonymous")

    # match the category ID with its display name
    category_name = utils.get_category_name(torrent["category"])
    torrent["category_name"] = f"{category_name} ({torrent["category"]})"

    # make the torrent size pretty
    torrent["size"] = utils.format_bytes(torrent["size"])

    # make the torrent last seen timestamp pretty
    torrent["last_seen"] = utils.time_ago(torrent["last_seen"])

    log.info(f"[VIEW] User '{user.user_label}' viewed torrent ID {torrent_id}")

    # display the HTML Jinja template with torrent object context
    return templates.TemplateResponse(name="view_torrent.html", context={"torrent": torrent, }, request=request)
