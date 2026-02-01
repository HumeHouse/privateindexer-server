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
        row = await mysql.fetch_one("SELECT id, label, downloaded, uploaded FROM users WHERE api_key = %s", (api_key,))
    elif user_id:
        row = await mysql.fetch_one("SELECT id, label, downloaded, uploaded FROM users WHERE id = %s", (user_id,))
    else:
        return None

    if not row:
        return None

    return User(row["id"], row["label"], api_key, row["downloaded"], row["uploaded"])
