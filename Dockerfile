FROM python:3.12-slim-bookworm AS sqlite-builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev ca-certificates && rm -rf /var/lib/apt/lists/*
COPY scripts/fetch_sqlite_source.py /tmp/fetch_sqlite_source.py
RUN python /tmp/fetch_sqlite_source.py /tmp/sqlite && mkdir /opt/sqlite && \
    gcc -O2 -fPIC -shared -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE \
    -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_MATH_FUNCTIONS /tmp/sqlite/sqlite3.c \
    -o /opt/sqlite/libsqlite3.so.0 -lm -ldl -pthread

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/AssassinLei/ClawIndex" \
      org.opencontainers.image.title="ClawIndex Gen2"
COPY --from=sqlite-builder /opt/sqlite /opt/sqlite
ENV LD_LIBRARY_PATH=/opt/sqlite PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    VNEXT_CLOUD=true VNEXT_DB_PATH=/data/quant_system_next.db TZ=Asia/Hong_Kong
RUN python -c "import sqlite3; assert sqlite3.sqlite_version_info == (3,51,3), sqlite3.sqlite_version"
WORKDIR /app
COPY requirements-next-tested.txt /app/requirements-next-tested.txt
RUN pip install --no-cache-dir -r requirements-next-tested.txt
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
