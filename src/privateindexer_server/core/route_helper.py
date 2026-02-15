import hashlib
import secrets
import time

from fastapi import Query, Form, Header, HTTPException
from fastapi.requests import Request

from privateindexer_server.core import logger
from privateindexer_server.core import user_helper
from privateindexer_server.core.user_helper import User


async def api_key_required(api_key_query: str | None = Query(None, alias="apikey"), api_key_form: str | None = Form(None, alias="apikey"),
                           api_key_header: str | None = Header(None, alias="X-API-Key"), ) -> User:
    """
    FastAPI depenedency to validate user API keys and return user data from database
    """
    api_key = api_key_query or api_key_form or api_key_header

    if not api_key:
        raise HTTPException(status_code=401, detail="API key missing")

    user = await user_helper.get_user(api_key=api_key)
    if not user:
        logger.channel("user").debug(f"Invalid API key used")
        raise HTTPException(status_code=403, detail="Invalid API key")
    return user


def latency_threshold(ms: int):
    """
    FastAPI dependency to set a custom high-latency threshold value for slower endpoints
    """

    async def set_latency_threshold(request: Request):
        request.state.latency_threshold = ms

    return set_latency_threshold


def generate_sid() -> str:
    """
    Generate a simple session ID
    """
    nonce = secrets.token_hex(16)
    raw = f"{nonce}:{time.time()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_client_ip(request: Request) -> str:
    """
    Helper to extract the IP address from a request
    """
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()

    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip

    return request.client.host
