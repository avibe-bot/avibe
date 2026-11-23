"""Prepare the release-pinned real sidecar for the Desktop CI interop test only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import tarfile
import urllib.request


def main() -> None:
    repo = Path(__file__).resolve().parents[3]
    manifest = json.loads((repo / 'vibe/direct_transport_runtime_manifest.json').read_text())
    system = {'Darwin': 'darwin', 'Windows': 'win32'}[platform.system()]
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}[platform.machine().lower()]
    entry = manifest['archives'][f'{system}-{arch}']
    tag = manifest['release_tag']
    name = entry['name']
    if (manifest['release_state'] != 'published' or not re.fullmatch(r'direct-transport-v[0-9.]+-[0-9]+', tag)
            or not re.fullmatch(r'avibe-direct-transport-[-a-zA-Z0-9_]+\.tar\.gz', name)):
        raise ValueError('invalid published fixture pin')
    url = f'https://github.com/avibe-bot/avibe/releases/download/{tag}/{name}'
    if entry['url'] != url or not 0 < entry['size'] <= 64 * 1024 * 1024:
        raise ValueError('invalid fixture URL or size')
    root = Path(os.environ['RUNNER_TEMP']) / 'desktop-direct-interop'
    root.mkdir(mode=0o700)
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read(entry['size'] + 1)
    if len(archive) != entry['size'] or hashlib.sha256(archive).hexdigest() != entry['sha256']:
        raise ValueError('fixture archive mismatch')
    binary = 'avibe-direct-transport' + ('.exe' if system == 'win32' else '')
    if entry['bin_path'] != binary:
        raise ValueError('unexpected fixture executable name')
    import io
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as tar:
        members = [m for m in tar if m.name == binary]
        if len(members) != 1 or not members[0].isreg() or not 0 < members[0].size <= 64 * 1024 * 1024:
            raise ValueError('invalid fixture member')
        with tar.extractfile(members[0]) as f:
            content = f.read()
    if hashlib.sha256(content).hexdigest() != entry['binary_sha256']:
        raise ValueError('fixture executable mismatch')
    exe = root / binary
    exe.write_bytes(content)
    exe.chmod(0o700)
    with Path(os.environ['GITHUB_ENV']).open('a', encoding='utf-8') as f:
        print(f'AVIBE_TEST_DIRECT_SIDECAR={exe}', file=f)


if __name__ == '__main__':
    main()
