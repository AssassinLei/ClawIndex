import hashlib
import io
import socket
import zipfile

import pytest

from scripts import fetch_sqlite_source as source


def test_real_bundled_sqlite_is_verified_and_prepared_without_network(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Offline SQLite preparation must not use the network')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    destination = tmp_path/'compiler-input'
    source.prepare(destination)
    assert {path.name for path in destination.iterdir()} == {'sqlite3.c', 'sqlite3.h'}
    for name, expected in source.MEMBER_SHA3.items():
        assert hashlib.sha3_256((destination/name).read_bytes()).hexdigest() == expected
    assert b'#define SQLITE_VERSION        "3.51.3"' in (destination/'sqlite3.h').read_bytes()


def test_missing_archive_fails_without_creating_output(tmp_path):
    destination = tmp_path/'compiler-input'
    with pytest.raises(FileNotFoundError, match='complete Gen2 checkout'):
        source.prepare(destination, tmp_path/'missing.zip')
    assert not destination.exists()


def test_corrupt_archive_is_rejected_before_existing_inputs_change(tmp_path):
    archive = tmp_path/'corrupt.zip'
    archive.write_bytes(source.ARCHIVE.read_bytes()+b'changed')
    destination = tmp_path/'compiler-input'
    destination.mkdir()
    (destination/'sqlite3.c').write_bytes(b'previous-input')
    with pytest.raises(RuntimeError, match='archive checksum mismatch'):
        source.prepare(destination, archive)
    assert (destination/'sqlite3.c').read_bytes() == b'previous-input'
    assert not (destination/'sqlite3.h').exists()


@pytest.mark.parametrize('member', ['sqlite3.c', 'sqlite3.h'])
def test_each_compiler_input_is_independently_checked_before_any_write(tmp_path, monkeypatch, member):
    stream = io.BytesIO()
    with zipfile.ZipFile(source.ARCHIVE) as official, zipfile.ZipFile(stream, 'w') as altered:
        for name in source.MEMBER_SHA3:
            path = 'sqlite-amalgamation-3510300/'+name
            altered.writestr(path, official.read(path)+(b'changed' if name == member else b''))
    data = stream.getvalue()
    archive = tmp_path/'altered.zip'
    archive.write_bytes(data)
    # Exercise the independent source check even if an archive pin were changed by mistake.
    monkeypatch.setattr(source, 'ARCHIVE_SHA256', hashlib.sha256(data).hexdigest())
    destination = tmp_path/'compiler-input'
    with pytest.raises(RuntimeError, match='does not match verified'):
        source.prepare(destination, archive)
    assert not destination.exists()
