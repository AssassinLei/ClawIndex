import pytest

from scripts.configure_build_sources import configure


@pytest.mark.parametrize('style', ['deb822', 'one-line'])
def test_mirror_preserves_suites_and_signatures_for_both_apt_formats(tmp_path, style):
    if style == 'deb822':
        path = tmp_path/'sources.list.d'/'debian.sources'
        content = ('Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm bookworm-updates\n'
                   'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n'
                   'Types: deb\nURIs: http://deb.debian.org/debian-security\nSuites: bookworm-security\n'
                   'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n')
    else:
        path = tmp_path/'sources.list'
        content = ('deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main\n'
                   'deb https://security.debian.org/debian-security bookworm-security main\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    configure(tmp_path, 'https://mirrors.tuna.tsinghua.edu.cn/')
    result = path.read_text(encoding='utf-8')
    assert 'https://mirrors.tuna.tsinghua.edu.cn/debian-security' in result
    assert 'https://mirrors.tuna.tsinghua.edu.cn/debian' in result
    for line in content.splitlines():
        if not line.startswith(('URIs:', 'deb ')):
            assert line in result
    assert result.count('debian-archive-keyring.gpg') == content.count('debian-archive-keyring.gpg')
    assert 'trusted=yes' not in result and 'Verify-Peer' not in result


def test_default_keeps_sources_unchanged(tmp_path):
    path = tmp_path/'sources.list'
    path.write_text('keep-default', encoding='utf-8')
    configure(tmp_path, '')
    assert path.read_text(encoding='utf-8') == 'keep-default'


@pytest.mark.parametrize('url', ['http://mirror.example', 'https://user:secret@mirror.example',
                             'https://mirror.example?token=value', 'https://mirror.example/#fragment'])
def test_invalid_mirror_never_rewrites_sources(tmp_path, url):
    path = tmp_path/'sources.list'
    path.write_text('deb http://deb.debian.org/debian bookworm main', encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(ValueError, match='HTTPS base URL'):
        configure(tmp_path, url)
    assert path.read_bytes() == before


def test_unknown_base_image_source_is_reported_instead_of_ignored(tmp_path):
    with pytest.raises(ValueError, match='No standard Debian source'):
        configure(tmp_path, 'https://mirrors.tuna.tsinghua.edu.cn')
