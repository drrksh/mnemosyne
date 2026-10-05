# Mnemosyne server image (team / company deployments).
#
#   docker build -t mnemosyne .
#   docker run -p 8420:8420 -e MNEMOSYNE_DATABASE_URL=postgresql+psycopg://... mnemosyne
#
# Defaults to api_key authentication on 0.0.0.0. Create the first
# administrator with:  docker exec -it <container> mnemosyne admin user create <handle> --admin

FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build==1.2.2
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
ARG UID=10001
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MNEMOSYNE_HOME=/data \
    MNEMOSYNE_API_HOST=0.0.0.0 \
    MNEMOSYNE_API_PORT=8420 \
    MNEMOSYNE_AUTH_MODE=api_key \
    MNEMOSYNE_LOG_FORMAT=json \
    MNEMOSYNE_HOOK_DAEMON=false \
    MNEMOSYNE_APP_ENV=production
RUN useradd --uid "${UID}" --create-home --shell /usr/sbin/nologin mnemosyne \
    && mkdir -p /data && chown mnemosyne /data && chmod 700 /data
COPY --from=build /dist/*.whl /tmp/
RUN wheel="$(ls /tmp/mnemosyne-*.whl)" \
    && pip install --no-cache-dir "${wheel}[postgres]" \
    && rm -f /tmp/*.whl
USER mnemosyne
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MNEMOSYNE_API_PORT', '8420'), timeout=4)"
# Migrations run at startup (MNEMOSYNE_AUTO_MIGRATE=true, the default). With
# several replicas, set it to false and run `mnemosyne db upgrade` as a
# release step instead.
CMD ["mnemosyne", "serve"]
