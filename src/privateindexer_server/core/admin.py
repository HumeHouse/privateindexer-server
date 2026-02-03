import time

from fastapi import Request, APIRouter, Form, HTTPException
from fastapi.params import Path
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from privateindexer_server.core import utils, admin_helper, user_helper
from privateindexer_server.core.logger import log

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="/app/src/templates")

SESSIONS = {}
# 30-day session lifetime
SESSION_TTL = 60 * 60 * 24 * 30


def validate_session(request: Request) -> bool:
    """
    Helper to validate admin sessions
    """
    # get the SID cookie
    sid = request.cookies.get("SID")

    # check if session cookie exists and is stored and not expired
    return sid is not None and sid in SESSIONS and time.time() < SESSIONS[sid]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Used to view the admin panel for authenticated users or direct to setup/login page
    """
    # if there is no password set, allow the user to create one
    if admin_helper.get_admin_password() is None:
        return templates.TemplateResponse(name="admin_setup.html", context={}, request=request)

    # check if session is valid
    if not validate_session(request):
        return templates.TemplateResponse(name="admin_login.html", context={}, request=request)

    log.info(f"[ADMIN] Admin panel viewed")

    return templates.TemplateResponse(name="admin_dashboard.html", context={}, request=request)


@router.post("/setup", response_class=HTMLResponse)
async def setup(request: Request, password: str = Form(...)):
    """
    Used to initially set up the admin password
    Admin password can only be set if none is stored
    """
    # make sure no password is set yet
    if admin_helper.get_admin_password() is not None:
        return RedirectResponse("/admin", status_code=302)

    # make sure the password meets requirements
    if not admin_helper.set_admin_password(password):
        return templates.TemplateResponse(name="admin_setup.html", context={"error": "Password doesn't meet requirements"}, request=request)

    log.info(f"[ADMIN] Admin passwor set")

    return RedirectResponse("/admin", status_code=302)


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(...)):
    """
    Allows for user auth and session creation if password matches
    """
    # if password is incorrect, display error
    if not admin_helper.verify_admin_password(password):
        return templates.TemplateResponse(name="admin_login.html", context={"error": "Invalid password"}, request=request)

    sid = utils.generate_sid()
    SESSIONS[sid] = time.time() + SESSION_TTL

    response = RedirectResponse("/admin", status_code=302)
    response.set_cookie(key="SID", value=sid, httponly=True, secure=False, samesite="strict", path="/admin")

    log.info(f"[ADMIN] Admin panel login succeeded")

    return response


@router.get("/users", response_class=HTMLResponse)
async def get_users(request: Request):
    """
    Retrieves a list of all users
    """
    # check if session is valid
    if not validate_session(request):
        raise HTTPException(status_code=401, detail="Invalid session")

    return JSONResponse(await user_helper.get_users())


@router.post("/user", response_class=HTMLResponse)
async def create_user(request: Request, user_label: str = Form(...)):
    """
    Creates a new user with the specified label
    """
    # check if session is valid
    if not validate_session(request):
        raise HTTPException(status_code=401, detail="Invalid session")

    await user_helper.create_user(user_label)

    log.info(f"[ADMIN] New user created: {user_label}")

    return PlainTextResponse("User created")


@router.delete("/user/{user_id}", response_class=HTMLResponse)
async def delete_user(request: Request, user_id: int = Path(...)):
    """
    Deletes a user from database if exists
    """
    # check if session is valid
    if not validate_session(request):
        raise HTTPException(status_code=401, detail="Invalid session")

    await user_helper.delete_user(user_id)

    log.info(f"[ADMIN] User ID deleted: {user_id}")

    return PlainTextResponse("User deleted")
