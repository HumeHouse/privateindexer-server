import asyncio
import datetime
import socket

from privateindexer_server.core import mysql
from privateindexer_server.core.config import CLIENT_CHECK_INTERVAL
from privateindexer_server.core.logger import log


async def check_clients():
    """
    Checks the connectivity of clients with valid IP and port
    """
    reachable = 0
    unreachable = 0
    unknown = 0

    # fetch all users
    users = await mysql.fetch_all("SELECT id, last_ip, reachable FROM users")

    # loop through each user
    for user in users:
        user_id = user["id"]
        last_ip = user.get("last_ip")
        last_status = user.get("reachable")

        # check if no IP is known for this user
        if last_ip is None:
            log.debug(f"[CLIENT-CHECK] User is in unknown status: {user_id}")
            unknown += 1

            # update status in database if necessary
            if last_status != -1:
                await mysql.execute("UPDATE users SET reachable = -1 WHERE id = %s", (user_id,))
            continue

        # split last_ip text for IP and port
        ip_address, port = last_ip.rsplit(":", 1)

        new_status = 0

        # try to connect to client
        try:
            with socket.create_connection((ip_address, port), timeout=5):
                reachable += 1
                new_status = 1
                log.debug(f"[CLIENT-CHECK] User is reachable: {user_id}")
        except (socket.timeout, ConnectionRefusedError, OSError):
            log.debug(f"[CLIENT-CHECK] User is unreachable: {user_id}")
            unreachable += 1
            pass

        # update status in database if necessary
        if new_status != last_status:
            await mysql.execute("UPDATE users SET reachable = %s WHERE id = %s", (new_status, user_id,))

    return reachable, unreachable, unknown


async def periodic_client_check_task():
    """
    Task to check user client reachability
    """
    log.debug("[CLIENT-CHECK] Task loop started")
    while True:
        try:
            log.debug("[CLIENT-CHECK] Running periodic client check")
            before = datetime.datetime.now()

            reachable, unreachable, unknown = await check_clients()

            delta = datetime.datetime.now() - before
            log.debug(f"[CLIENT-CHECK] Client check complete ({delta}): {reachable} reachable, {unreachable} unreachable, {unknown} unknown")
        except Exception as e:
            log.error(f"[CLIENT-CHECK] Error during periodic client check: {e}")
        await asyncio.sleep(CLIENT_CHECK_INTERVAL)
