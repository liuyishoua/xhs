"""Publish assets before HTML, retaining old chunks and their exact module URLs.

After building into an isolated directory:
sudo python3 deploy/publish_frontend.py /absolute/build/output
"""
from pathlib import Path
import os
import re
import shutil
import sys


def publish(build: Path, served: Path, public: Path):
    html = (build / 'index.html').read_text()
    assets = list((build / 'assets').iterdir())
    for ref in re.findall(r'(?:src|href)="(/assets/[^"?]+)', html):
        if not (build / ref.lstrip('/')).is_file():
            raise ValueError('Build references a missing asset: ' + ref)
    (public / 'assets').mkdir(parents=True, exist_ok=True)
    (served / 'assets').mkdir(parents=True, exist_ok=True)
    for asset in assets:
        if not asset.is_file():
            continue
        for target in (public / 'assets' / asset.name, served / 'assets' / asset.name):
            if target.resolve() != asset.resolve():
                shutil.copy2(asset, target)
            target.chmod(0o644)
    for target in (public, served):
        for icon in ('ops-icon.svg', 'favicon.svg', 'platform-brand.ico', 'workspace-theme.js'):
            if (build / icon).is_file():
                shutil.copy2(build / icon, target / icon)
                (target / icon).chmod(0o644)
        temporary = target / 'index.html.next'
        temporary.write_text(html)
        temporary.chmod(0o644)
        os.replace(temporary, target / 'index.html')
    print(f'Published {len(assets)} assets; retained older chunks and original module URLs')


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    publish(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / 'frontend/dist',
            root / 'frontend/dist', Path('/srv/xhs-ui'))
