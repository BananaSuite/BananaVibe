"""Download the pinned OpenCode binary and verify its release digest."""
import argparse
import hashlib
from pathlib import Path
import platform
import tarfile
import tempfile
from urllib.request import urlopen

VERSION = '1.18.18'
ASSETS = {
    'x86_64': ('opencode-linux-x64-baseline.tar.gz', '05914655854c1dab48f0d52a8af8f74967e4e26daf87dfe6534f57e799a853d7'),
    'aarch64': ('opencode-linux-arm64.tar.gz', 'dcb1b5ec5687b43f87749560021f9203f3809e0ce5ae44ff9be8ae17083fe4ba'),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    if platform.machine() not in ASSETS:
        parser.error('Supported architectures are x86_64 and aarch64.')
    asset, expected = ASSETS[platform.machine()]
    url = f'https://github.com/anomalyco/opencode/releases/download/v{VERSION}/{asset}'
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / asset
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as source, archive.open('wb') as target:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != expected:
            raise RuntimeError('The OpenCode release checksum does not match.')
        with tarfile.open(archive) as source:
            members = [member for member in source.getmembers() if Path(member.name).name == 'opencode' and member.isfile()]
            if len(members) != 1:
                raise RuntimeError('The release does not contain the expected binary.')
            args.destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(members[0]) as binary, args.destination.open('wb') as target:
                while chunk := binary.read(1024 * 1024):
                    target.write(chunk)
            args.destination.chmod(0o755)
    print(f'Installed OpenCode {VERSION} at {args.destination}')


if __name__ == '__main__':
    main()
