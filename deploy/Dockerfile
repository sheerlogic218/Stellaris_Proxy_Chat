# Images for the central server: two targets, each with only its own code and packages.
#
#   docker build -f deploy/Dockerfile --target api -t proxychat-api .
#   docker build -f deploy/Dockerfile --target bot -t proxychat-bot .
#
# (docker compose in deploy/ builds both.) Pin the base image by digest before going
# live: set PYTHON_IMAGE=python:3.14-slim@sha256:<digest> in deploy/.env
# (see docs/DEPLOYMENT_PLACEHOLDERS.md).

ARG PYTHON_IMAGE=python:3.14-slim

# ---- dependencies: hash-checked, in their own virtual environments ----------------

FROM ${PYTHON_IMAGE} AS deps-api
COPY requirements/api.txt /tmp/api.txt
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/api.txt \
 && /venv/bin/pip uninstall --yes pip

FROM ${PYTHON_IMAGE} AS deps-bot
COPY requirements/bot.txt /tmp/bot.txt
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/bot.txt \
 && /venv/bin/pip uninstall --yes pip

# ---- shared runtime: no pip, a fixed unprivileged user ----------------------------

FROM ${PYTHON_IMAGE} AS runtime
RUN python -m pip uninstall --yes pip setuptools wheel 2>/dev/null; \
    useradd --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin proxychat
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH
WORKDIR /app

# ---- the API: on the internet (through Tailscale Funnel), no Discord code or token ----

FROM runtime AS api
COPY --from=deps-api /venv /venv
COPY proxychat_core/ /app/proxychat_core/
COPY proxychat_server/__init__.py proxychat_server/api.py proxychat_server/config.py \
     proxychat_server/db.py proxychat_server/ratelimit.py /app/proxychat_server/
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"]
CMD ["python", "-m", "proxychat_server.api", "--config", "/config/server.toml"]

# ---- the bot: talks to Discord, no API code, nothing listens ----------------------

FROM runtime AS bot
COPY --from=deps-bot /venv /venv
COPY proxychat_core/ /app/proxychat_core/
COPY proxychat_bot/ /app/proxychat_bot/
COPY proxychat_server/__init__.py proxychat_server/__main__.py proxychat_server/admin_cli.py \
     proxychat_server/config.py proxychat_server/db.py proxychat_server/queue.py \
     proxychat_server/serverbot.py /app/proxychat_server/
COPY deploy/healthcheck_bot.py /app/healthcheck_bot.py
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD ["python", "/app/healthcheck_bot.py"]
CMD ["python", "-m", "proxychat_server", "--config", "/config/server.toml"]
