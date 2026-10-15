"""Unpack bundled official SQLite source offline, checking archive and both inputs."""
import hashlib
import io
import sys
import zipfile
from pathlib import Path


ARCHIVE = Path(__file__).resolve().parent/'vendor'/'sqlite-amalgamation-3510300.zip'
ARCHIVE_SHA256 = 'acb1e6f5d832484bf6d32b681e858c38add8b2acdfd42ac5df24b8afb46552b4'
MEMBER_SHA3 = {
    'sqlite3.c': '32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e',
    'sqlite3.h': 'f6af1852a4d3c5a32f55ff383ca41b83c2eb78e8405784bb452a4c063c266170',
}


def prepare(destination, archive_path=None):
    path = Path(archive_path) if archive_path is not None else ARCHIVE
    if not path.is_file():
        raise FileNotFoundError('Bundled SQLite archive missing; update the complete Gen2 checkout')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError('Bundled SQLite archive checksum mismatch; refusing to compile')
    verified = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name, expected in MEMBER_SHA3.items():
            content = archive.read('sqlite-amalgamation-3510300/'+name)
            if hashlib.sha3_256(content).hexdigest() != expected:
                raise RuntimeError('SQLite '+name+' does not match verified 3.51.3 source')
            verified[name] = content
    # Validate every input before publishing any compiler input; never use a network fallback.
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in verified.items():
        (destination/name).write_bytes(content)
    print('Verified bundled SQLite 3.51.3 source (offline)', flush=True)


def main():
    prepare(sys.argv[1])


if __name__ == '__main__':
    main()
