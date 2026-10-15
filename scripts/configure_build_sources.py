"""Select an optional Debian mirror without changing suites or signature checks."""
import sys
from pathlib import Path
from urllib.parse import urlsplit


def configure(apt_root, mirror):
    mirror = mirror.strip().rstrip('/')
    if not mirror:
        print('APT: using image default sources', flush=True)
        return
    url = urlsplit(mirror)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('APT mirror must be an HTTPS base URL without credentials, query or fragment')
    root = Path(apt_root)
    paths = [root/'sources.list', *sorted((root/'sources.list.d').glob('*.list')),
             *sorted((root/'sources.list.d').glob('*.sources'))]
    changed = []
    for path in paths:
        if not path.is_file():
            continue
        original = path.read_text(encoding='utf-8')
        content = original
        # Longest paths first; otherwise /debian would also match /debian-security.
        for source, target in (
            ('deb.debian.org/debian-security', mirror+'/debian-security'),
            ('security.debian.org/debian-security', mirror+'/debian-security'),
            ('deb.debian.org/debian', mirror+'/debian'),
        ):
            for scheme in ('http://', 'https://'):
                content = content.replace(scheme+source, target)
        if content != original:
            changed.append((path, content))
    if not changed:
        raise ValueError('No standard Debian source found; refusing to ignore the requested mirror')
    for path, content in changed:
        path.write_text(content, encoding='utf-8')
    print('APT: configured mirror '+mirror, flush=True)


if __name__ == '__main__':
    configure('/etc/apt', sys.argv[1] if len(sys.argv)>1 else '')
