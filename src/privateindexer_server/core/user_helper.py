import secrets

from privateindexer_server.core import mysql


class User:
    """
    Helper class to store user information
    """
    user_id: int
    user_label: str
    api_key: str
    downloaded: int
    uploaded: int

    def __init__(self, user_id: int, user_label: str, api_key: str, downloaded: int, uploaded: int):
        self.user_id: int = user_id
        self.user_label: str = user_label
        self.api_key: str = api_key
        self.downloaded: int = downloaded
        self.uploaded: int = uploaded


async def get_user(api_key: str = None, user_id: int = None) -> User | None:
    """
    Fetch user data based on API key or user ID
    """

    if api_key:
        where_clause = "WHERE api_key = %s"
        where_params = (api_key,)
    elif user_id:
        where_clause = "WHERE id = %s"
        where_params = (user_id,)
    else:
        return None

    row = await mysql.fetch_one(f"SELECT id, label, api_key, downloaded, uploaded FROM users {where_clause}", where_params)

    if not row:
        return None

    return User(row["id"], row["label"], row["api_key"], row["downloaded"], row["uploaded"])


async def get_users() -> list[dict]:
    """
    Fetch all users from database
    """
    return await mysql.fetch_all("SELECT * FROM users")


async def create_user(user_label: str):
    """
    Adds a new user with a generated API key
    """
    api_key = secrets.token_hex(32)

    await mysql.execute("INSERT INTO users (label, api_key) VALUES (%s, %s)", (user_label, api_key,))


async def delete_user(user_id: int = None):
    """
    Removes a user based on the user ID
    """

    await mysql.execute("DELETE FROM users WHERE id = %s", (user_id,))
