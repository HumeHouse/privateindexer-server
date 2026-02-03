import os

import bcrypt

from privateindexer_server.core.config import ADMIN_PASSWORD_FILE
from privateindexer_server.core.logger import log


def get_admin_password() -> str | None:
    """
    Helper to get password hash from file if exists
    """
    # return nothing if file doesn't exist
    if not os.path.exists(ADMIN_PASSWORD_FILE):
        return None

    # if file does exist, try to read the key
    try:
        with open(ADMIN_PASSWORD_FILE, "r") as f:
            return f.read()
    except Exception as e:
        log.error(f"[ADMIN] Exception while loading admin.password: {e}")
        return None


def verify_admin_password(admin_password: str) -> bool:
    """
    Helper to check for admin password match with configured admin password
    """
    # get the stored admin password
    stored_admin_password = get_admin_password()

    return bcrypt.checkpw(admin_password.encode(), stored_admin_password.encode())


def set_admin_password(admin_password: str) -> bool:
    """
    Helper to save the admin password to file
    """
    # require at least twelve characters in length
    if len(admin_password) < 12:
        return False

    # require at least one capital letter
    if sum(1 for c in admin_password if c.isupper()) < 1:
        return False

    # require at least one number
    if sum(1 for c in admin_password if c.isnumeric()) < 1:
        return False

    # use bcrypt for secure hashing
    hashed_password = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt())
    hashed_password = hashed_password.decode()

    # save password hash to file
    try:
        with open(ADMIN_PASSWORD_FILE, "w") as f:
            f.write(hashed_password)
    except Exception as e:
        log.error(f"[ADMIN] Exception while saving admin.password: {e}")

    return True
