"""Fetch a pinned SQLite amalgamation; validate sqlite3.c against release SHA3."""
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path


def main():
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True,exist_ok=True)
    with urllib.request.urlopen('https://www.sqlite.org/2026/sqlite-amalgamation-3510300.zip',timeout=60) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    source = archive.read('sqlite-amalgamation-3510300/sqlite3.c')
    expected = '32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e'
    if hashlib.sha3_256(source).hexdigest() != expected:
        raise RuntimeError('SQLite source does not match official 3.51.3 release')
    (destination/'sqlite3.c').write_bytes(source)
    (destination/'sqlite3.h').write_bytes(archive.read('sqlite-amalgamation-3510300/sqlite3.h'))
    print('Verified SQLite 3.51.3 source')


if __name__ == '__main__':
    main()
