# PrivateIndexer Server

This is the server container for the HumeHouse PrivateIndexer swarm system.
It functions as the centralized storage for torrents sent from clients and serves search queries to return results and
provide access to torrent files.
A built-in admin dashboard allows for easy user management and viewing statistics.

**NOTE:** This is a `non-root`/`rootless` container, make sure the permissions on your system match your configuration.

---

## Building

Clone this repository and simply run the following command in the directory with the `Dockerfile` and your image will be
built

```bash
docker compose build
```

You can also use the hosted pre-built image on at `ghcr.io/humehouse/privateindexer-server:latest` like in the example
below
(See [GitHub](https://github.com/HumeHouse/privateindexer-client/tags) for all version tags)

---

## Quick Start Example (using Docker)

### Use the [example docker-compose.yml](docker-compose.yml) and adjust paths and environment variables to match your setup.

Here’s an example setup:

- My persistent data (torrents and database) for the server is stored in `/humehouse/privateindexer` on the host
- The MySQL and Redis server containers I use are within the same Docker network as the PrivateIndexer server container.

### 1. Configure Environment Variables

#### REQURIED VARIABLES

| Variable               | Description                                                                                                        | Example                         |
|------------------------|--------------------------------------------------------------------------------------------------------------------|---------------------------------|
| `REDIS_HOST`           | Redis server hostname for storing peer connections and server analytics.                                           | `privateindexer-redis`          |
| `MYSQL_HOST`           | MySQL server hostname for storing torrent metadata and maintaining user database.                                  | `privateindexer-mysql`          |
| `MYSQL_ROOT_PASSWORD`  | Root password to use for setting up the schema and user on the MySQL server.                                       | `privateindexer`                |
| `EXTERNAL_TRACKER_URL` | Externally accessible URL pointing to your server instance to respond to API requests and track uploaded torrents. | `https://indexer.humehouse.com` |
| `ANNOUNCE_TRACKER_URL` | Externally accessible URL pointing to your tracker instance to receive announcement requests.                      | `https://tracker.humehouse.com` |

#### OPTIONAL VARIABLES

| Variable                  | Default Value              | Type                     | Description                                                                                                                                 |
|---------------------------|----------------------------|--------------------------|---------------------------------------------------------------------------------------------------------------------------------------------|
| `PEER_TIMEOUT`            | `1800`                     | `INTEGER` (seconds)      | Number of seconds since the last tracker announcement to consider a peer as dead and remove from Redis.                                     |
| `PEER_TIMEOUT_INTERVAL`   | `1`                        | `INTEGER` (minutes)      | How often to check for and purge old peers from Redis.                                                                                      |
| `STATS_UPDATE_INTERVAL`   | `30`                       | `INTEGER` (seconds)      | How often to update user statistics in MySQL based on data from Redis peers. This data is served to users via API for showing server stats. |
| `HIGH_LATECY_THRESHOLD`   | `250`                      | `INTEGER` (milliseconds) | Threshold or tolerance for latency during API request processing. Any request duration over this limit will print a warning in the logs.    |
| `DATABASE_CHECK_INTERVAL` | `12`                       | `INTEGER` (hours)        | How often to check the database for missing or invalid torrents. Invalid torrents will also be purged during this task.                     |
| `STALE_THRESHOLD`         | `30`                       | `INTEGER` (days)         | How long to consider a torrent with no peers as stale to be removed by the stale check task.                                                |
| `STALE_CHECK_INTERVAL`    | `6`                        | `INTEGER` (hours)        | How often to check for stale torrents to be purged from the database and disk.                                                              |
| `SYNC_BATCH_SIZE`         | `5000`                     | `INTEGER`                | Number of torrents to process at a time from a user sync request.                                                                           |
| `ACCESS_TOKEN_EXPIRATION` | `10`                       | `INTEGER` (minutes)      | How long after JWT AT generation an access token is valid for an can be used for viewing or grabbing torrents from the API.                 |
| `SITE_NAME`               | `HumeHouse PrivateIndexer` | `TEXT`                   | This is the title shown on the `/view` endpoint shown to users and in the XML returned by API endpoints.                                    |
| `MYSQL_PORT`              | `3306`                     | `INTEGER` (port)         | Port number to use when connecting to MySQL server.                                                                                         |
| `MYSQL_USER`              | `privateindexer`           | `TEXT`                   | Username to authenticate with MySQL server.                                                                                                 |
| `MYSQL_PASSWORD`          | `privateindexer`           | `TEXT`                   | Password to use for authenticating the user to with MySQL server.                                                                           |
| `MYSQL_DB`                | `privateindexer`           | `TEXT`                   | Name of the MySQL schema or database to connect to and use for data storage.                                                                |
| `LOG_LEVEL`               | `INFO`                     | `KEYWORD` (level)        | Lowest log level to show in console. Can be `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` where `DEBUG` shows most amount of logs.     |
| `TZ`                      | `America/Chicago`          | `TEXT` (ISO 8601)        | Change this to your desired time zone. Check online for a list of ISO 8601 time zones.                                                      |
| `UID`                     | `1000`                     | `INTEGER` (user)         | User ID on the system to run the app as. Make sure this user can read and write to the `app data` directory.                                |
| `GID`                     | `1000`                     | `INTEGER` (group)        | Group ID on the system to run the app as. Make sure this group can read and write to the `app data` directory                               |

### 2. Configure Volumes

**NOTE:** Make sure the `/app/data` mountpoint is readable and writable by your configured `UID:GID`, otherwise the
container will fail to start.
Normally this just means 'don't create the directories as root' as a general rule of thumb.

| Volume      | Description                                                                                        | Example                                           |
|-------------|:---------------------------------------------------------------------------------------------------|---------------------------------------------------|
| `/app/data` | Persistent storage inside the app for storing torrent files (.torrent) and the persistent JWT key. | `/humehouse/privateindexer/server/data:/app/data` |

#### Optional log file mount

You can choose whether to mount `/app/logs` somewhere on the host if you would like to have persistent log files saved.

### 3. Port forwarding

The Webserver Port

- The API web server runs on port 8080 inside the container to expose the RESTful API which clients use for various
  purposes.
- You can map the web server port to any port on the host or none at all if you connect from within the Docker
  network, such as using a reverse proxy like NGINX.

### 4. Start Server

Start container:

```bash
docker compose up -d
```

View logs and follow console:

```bash
docker compose logs server -f
```

### 5. Set up the admin panel

Browse to `http://hostname:8080/admin` to set up the admin password.

Password requirements:

- At least 12 characters in length
- Must contain at least 1 number
- Must contain at least 1 uppercase letter
- Must contain at least 1 lowercase letter

Once the password has been set, the panel will redirect to the login page. Log in using your newly created password.

### 6. Create a new user

You can create new users using the `Create User` button and assigning a display label (shown to other users on `/view`)

Click the clipboard to copy a user's API key to the clipboard and send the API key to the user for use in their client.

Delete users by clicking `Delete` in the actions column of the user's row in the table.

### 7. Run the tracker

Visit the [privateindexer-tracker](https://github.com/HumeHouse/privateindexer-tracker) repository to complete the
tracker setup process to start listening for torrent announcments.
