import asyncio
import datetime
import socket

from privateindexer_server.core import mysql
from privateindexer_server.core.config import CLIENT_CHECK_INTERVAL
from privateindexer_server.core import logger


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
            logger.channel("client-check").debug(f"User is in unknown status: {user_id}")
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
                logger.channel("client-check").debug(f"User is reachable: {user_id}")
        except (socket.timeout, ConnectionRefusedError, OSError):
            logger.channel("client-check").debug(f"User is unreachable: {user_id}")
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
    logger.channel("client-check").debug("Task loop started")
    while True:
        try:
            logger.channel("client-check").debug("Running periodic client check")
            before = datetime.datetime.now()

            reachable, unreachable, unknown = await check_clients()

            delta = datetime.datetime.now() - before
            logger.channel("client-check").debug(f"Client check complete ({delta}): {reachable} reachable, {unreachable} unreachable, {unknown} unknown")
        except Exception as e:
            logger.channel("client-check").error(f"Error during periodic client check: {e}")
        await asyncio.sleep(CLIENT_CHECK_INTERVAL)
