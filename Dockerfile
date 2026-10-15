FROM python:3.12-slim-bookworm AS sqlite-builder
ARG APT_MIRROR=""
COPY scripts/configure_build_sources.py /tmp/configure_build_sources.py
RUN python /tmp/configure_build_sources.py "$APT_MIRROR" && \
    apt-get -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 -o APT::Update::Error-Mode=any update && \
    apt-get -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2 install -y --no-install-recommends gcc libc6-dev ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY scripts/fetch_sqlite_source.py /tmp/fetch_sqlite_source.py
RUN python /tmp/fetch_sqlite_source.py /tmp/sqlite && mkdir /opt/sqlite && \
    gcc -O2 -fPIC -shared -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE \
    -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_MATH_FUNCTIONS /tmp/sqlite/sqlite3.c \
    -o /opt/sqlite/libsqlite3.so.0 -lm -ldl -pthread
COPY requirements-next-tested.txt /tmp/requirements-next-tested.txt
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --timeout 30 --retries 3 -r /tmp/requirements-next-tested.txt

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/AssassinLei/ClawIndex" \
      org.opencontainers.image.title="ClawIndex Gen2"
COPY --from=sqlite-builder /opt/sqlite /opt/sqlite
COPY --from=sqlite-builder /opt/venv /opt/venv
ENV LD_LIBRARY_PATH=/opt/sqlite PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VNEXT_CLOUD=true VNEXT_DB_PATH=/data/quant_system_next.db TZ=Asia/Hong_Kong \
    PATH=/opt/venv/bin:$PATH
RUN python -c "import sqlite3; assert sqlite3.sqlite_version_info == (3,51,3), sqlite3.sqlite_version"
WORKDIR /app
COPY requirements-next-tested.txt /app/requirements-next-tested.txt
RUN groupadd --gid 10001 clawindex && useradd --uid 10001 --gid 10001 --create-home clawindex && \
    mkdir -p /data /backups && chown clawindex:clawindex /data /backups
COPY clawindex_next /app/clawindex_next
COPY app_next.py start_next.py /app/
COPY .streamlit /app/.streamlit
COPY scripts /app/scripts
USER clawindex
EXPOSE 8502
ENTRYPOINT ["python", "scripts/cloud_entry.py"]
CMD ["web"]
