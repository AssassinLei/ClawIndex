# SQLite build source

`sqlite-amalgamation-3510300.zip` is the unmodified official SQLite 3.51.3 source
archive, bundled so deployment builds do not require access to sqlite.org.

- Source: https://www.sqlite.org/2026/sqlite-amalgamation-3510300.zip
- Release: https://www.sqlite.org/releaselog/3_51_3.html
- Archive SHA-256: `acb1e6f5d832484bf6d32b681e858c38add8b2acdfd42ac5df24b8afb46552b4`
- `sqlite3.c` SHA3-256 (published in the official release notes):
  `32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e`
- `sqlite3.h` SHA3-256 (from the same official archive):
  `f6af1852a4d3c5a32f55ff383ca41b83c2eb78e8405784bb452a4c063c266170`

SQLite deliverable source is public domain:
https://www.sqlite.org/copyright.html

`scripts/fetch_sqlite_source.py` checks the archive and both compilation inputs
before writing them. No automatic network download, TLS bypass or version downgrade
is used. Updating SQLite requires deliberately updating the archive, pinned hashes
and Docker runtime version assertion together.
