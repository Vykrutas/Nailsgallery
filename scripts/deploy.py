"""Publish only the Nails Gallery page and versioned public assets."""
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid

SITE = Path(__file__).resolve().parents[1] / 'site'
ROOT = '/var/www/artnails/data/www/nailsgallery.ru'
HOST = 'deploy_nails@94.250.255.207'
URL = 'https://nailsgallery.ru/'

class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ('src', 'href', 'data-src') and value and value.startswith('nails-assets/'):
                self.assets.add(value)

def validate():
    page = (SITE / 'index.html').read_text(encoding='utf-8')
    refs = References()
    refs.feed(page)
    assert 'nails-assets/NailsGallery_logo_v2.svg' in refs.assets, 'Missing new logo'
    assert 'nails-assets/NailsGallery_favicon.svg' in refs.assets, 'Missing favicon'
    for ref in refs.assets:
        path = SITE / ref
        assert path.is_file() and path.stat().st_size, f'Missing asset: {ref}'
    assets = sorted((SITE / 'nails-assets').rglob('*'))
    for path in assets:
        assert not path.is_symlink(), f'Symlink not allowed: {path}'
        relative = path.relative_to(SITE / 'nails-assets').as_posix()
        assert re.fullmatch(r'[A-Za-z0-9_./-]+', relative), f'Unsupported filename: {relative}'
    return page, [path for path in assets if path.is_file()]

def fetch_equal(url, expected, image=False):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                assert response.status == 200, f'HTTP error: {url}'
                if image:
                    assert response.headers.get_content_type().startswith('image/'), f'Not an image: {url}'
                assert response.read() == expected, f'Content mismatch: {url}'
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)

def main():
    page, assets = validate()
    print(f'Local checks passed: {len(assets)} assets', flush=True)
    if '--check' in sys.argv:
        return
    key = os.environ.pop('DEPLOY_SSH_KEY', '')
    known = os.environ.pop('DEPLOY_KNOWN_HOSTS', '')
    if not key.strip() or not known.strip():
        raise RuntimeError('Configure DEPLOY_SSH_KEY and DEPLOY_KNOWN_HOSTS in GitHub Actions secrets')
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
    asset_dir = 'nails-assets-' + stamp
    new_page = page.replace('nails-assets/', asset_dir + '/').encode('utf-8')
    backup = '/home/deploy_nails/nailsgallery-backups/' + stamp
    stage = ROOT + '/.index-' + stamp + '.html'
    with tempfile.TemporaryDirectory(prefix='nails-deploy-') as folder:
        tmp = Path(folder)
        key_path, known_path = tmp / 'key', tmp / 'known_hosts'
        key_path.write_text(key.strip() + '\n', encoding='utf-8')
        known_path.write_text(known.strip() + '\n', encoding='utf-8')
        key_path.chmod(0o600)
        common = ['-F', '/dev/null', '-i', str(key_path), '-o', 'IdentitiesOnly=yes',
                  '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=yes',
                  '-o', 'UserKnownHostsFile=' + str(known_path)]
        def run(args):
            subprocess.run(args, check=True, timeout=180)
        def remote(command):
            run(['ssh', *common, HOST, 'set -eu; ' + command])
        remote(f'test -w {ROOT}; test -f {ROOT}/index.html; test ! -L {ROOT}/index.html; '
               f'mkdir -m 755 {ROOT}/{asset_dir}')
        run(['scp', *common, '-r', str(SITE / 'nails-assets') + '/.', f'{HOST}:{ROOT}/{asset_dir}/'])
        remote(f'find {ROOT}/{asset_dir} -type d -exec chmod 755 {{}} +; '
               f'find {ROOT}/{asset_dir} -type f -exec chmod 644 {{}} +')
        for asset in assets:
            relative = asset.relative_to(SITE / 'nails-assets').as_posix()
            fetch_equal(URL + asset_dir + '/' + relative, asset.read_bytes(), image=True)
        local_page = tmp / 'index.html'
        local_page.write_bytes(new_page)
        run(['scp', *common, str(local_page), HOST + ':' + stage])
        digest = hashlib.sha256(new_page).hexdigest()
        remote(f'mkdir -p -m 700 {backup}; cp -p {ROOT}/index.html {backup}/index.html; '
               f'chmod 644 {stage}; mv {stage} {ROOT}/index.html')
        try:
            fetch_equal(URL + '?deploy=' + stamp, new_page)
        except Exception:
            # Do not overwrite a page published meanwhile by another process.
            remote(f'printf "%s\\n" {shlex.quote(digest + "  " + ROOT + "/index.html")} | sha256sum -c -; '
                   f'cp {backup}/index.html {stage}; chmod 644 {stage}; mv {stage} {ROOT}/index.html')
            raise RuntimeError('Public verification failed; previous page restored')
        print('DEPLOY_OK: ' + URL, flush=True)
        print('Backup: ' + backup + '/index.html', flush=True)

if __name__ == '__main__':
    main()
