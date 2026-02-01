# first image stage is the base python system
FROM python:3.13.9-slim AS base
LABEL Description="Server container for the HumeHouse PrivateIndexer swarm"


# second image stage is for dependencies and source code
FROM base AS builder

WORKDIR /app

# copy python requirements file
COPY requirements.txt /app

# install dependencies
RUN pip install -r requirements.txt


# next image stage is for runtime (non-root)
FROM base AS runner

WORKDIR /app

# set the container user/group, timezone, and python environment
ENV UID=1000 \
    GID=1000 \
    TZ=America/Chicago \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# create user/group inside container
RUN groupadd -g ${GID} privateindexer \
 && useradd  -u ${UID} -g ${GID} -m -s /bin/bash privateindexer

# copy installed python packages from the builder image
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages

# copy binaries from the builder image
COPY --from=builder /usr/local/bin /usr/local/bin

# copy all source code
COPY --chown=privateindexer:privateindexer src/ /app/src

# copy logging config
COPY --chown=privateindexer:privateindexer logging.yml /app

# create data directories with open permissions
RUN mkdir -m 777 /app/data \
 && chown -R privateindexer:privateindexer /app/data

# create log directories with open permissions
RUN mkdir -m 777 /app/logs \
 && chown -R privateindexer:privateindexer /app/logs

# add the healthcheck to hit the app's health endpoint
HEALTHCHECK --start-period=30s --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request, sys; \
    sys.exit(0) if urllib.request.urlopen('http://localhost:8080/health').getcode() == 200 else sys.exit(1)"

# run app as container user/group
USER privateindexer:privateindexer

# open default webserver port
EXPOSE 8080

# change directories into source code for running
WORKDIR /app/src

# run the app
ENTRYPOINT ["uvicorn", "privateindexer_server.main:app", "--proxy-headers", "--workers=1", "--host=0.0.0.0", "--port=8080", "--log-config=/app/logging.yml"]