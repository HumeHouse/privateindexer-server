import base64
import os
import uuid
from datetime import datetime, timezone, timedelta

import jwt
from fastapi import Query, HTTPException

from privateindexer_server.core import user_helper
from privateindexer_server.core.config import ACCESS_TOKEN_EXPIRATION, JWT_KEY_FILE
from privateindexer_server.core.logger import log
from privateindexer_server.core.user_helper import User

JWT_OPTIONS = {
    "require": ["exp", "sub", "for", "aud"]
}
_jwt_key = None


class AccessTokenValidator:
    def __init__(self, purpose: str):
        """
        Initialize class with static purpose property
        """
        self.purpose = purpose

    async def __call__(self, access_token: str | None = Query(None, alias="at")) -> User:
        """
        Makes this class callable to be used as a dynamic FastAPI dependency
        """
        if not access_token:
            raise HTTPException(status_code=401, detail="Access token missing")

        # validate the token and check against the static purpose property
        user_id = validate_access_token(access_token, self.purpose)

        if user_id == -1:
            log.warning(f"[USER] Invalid or expired access token used: {access_token}")
            raise HTTPException(status_code=401, detail="Invalid or expired access token")

        user = await user_helper.get_user(user_id=user_id)
        if not user:
            log.warning(f"[USER] Invalid user ID: {user_id}")
            raise HTTPException(status_code=401, detail="Invalid or expired access token")
        return user


def get_jwt_key() -> str:
    """
    Helper to get or create JWT key file content
    """
    global _jwt_key
    # check if key is cached
    if _jwt_key:
        return _jwt_key

    # create the file if it doesn't exist and return new key
    if not os.path.exists(JWT_KEY_FILE):
        # generate a key
        _jwt_key = os.urandom(32).hex()

        with open(JWT_KEY_FILE, "w") as f:
            f.write(_jwt_key)

        log.debug(f"[JWT] Created new JWT key and saved to disk")
        return _jwt_key

    # if file does exist, try to read the key
    try:
        with open(JWT_KEY_FILE, "r") as f:
            _jwt_key = f.read()
    except Exception as e:
        log.error(f"[JWT] Exception while loading jwt.key: {e}")
        _jwt_key = None

    return _jwt_key


def create_access_token(user_id: int, purpose: str) -> str:
    """
    Creates a JWT access token using the user ID and purpose
    """
    payload = {
        "sub": (base64.b64encode(str(user_id).encode())).decode(),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRATION),
        "for": purpose,
        "aud": "acc",
        "jti": str(uuid.uuid4())
    }
    return jwt.encode(payload, get_jwt_key())


def validate_access_token(access_token: str, purpose: str) -> int:
    """
    Helper to validate and decode JWT access token payload, returning user ID
    """
    if access_token is None:
        return -1
    try:
        payload = jwt.decode(access_token, get_jwt_key(), options=JWT_OPTIONS, audience="acc", algorithms=["HS256"])

        # make sure purpose of token matches the request
        if payload.get("for") != purpose:
            return -1

        decoded = base64.decodebytes(payload.get("sub").encode())
        return decoded.decode()
    except jwt.PyJWTError as e:
        print(e)
        return -1
