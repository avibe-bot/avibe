#!/usr/bin/env python3
"""Build authenticated desktop update manifests; never treat .SIGNATURE as a signature."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from runpy import run_path
import tarfile

release = run_path(str(Path(__file__).with_name('desktop_release.py')))
TARGETS = {
    'aarch64-apple-darwin': ('darwin-aarch64', '.app.tar.gz'),
    'x86_64-apple-darwin': ('darwin-x86_64', '.app.tar.gz'),
    'x86_64-pc-windows-msvc': ('windows-x86_64', '.exe'),
}
REPOSITORY = 'avibe-bot/avibe'
ROOT = Path(__file__).resolve().parents[1]


def names(version: str, target: str) -> set[str]:
    manifest = f'desktop-update-{target}.json'
    return {f'Avibe_{version}_{target}{TARGETS[target][1]}', manifest, manifest + '.sig'}


def configuration(enabled: bool, public_key: str, private_key: str) -> None:
    if enabled and (not public_key.strip() or not private_key.strip()):
        raise ValueError('Enabled updater requires DESKTOP_UPDATER_PUBLIC_KEY and TAURI_SIGNING_PRIVATE_KEY')
    if '\n' in public_key or '\r' in public_key:
        raise ValueError('Updater public key must be the single-line Tauri base64 public key')


def configure(config: Path, env_file: Path, enabled: bool) -> None:
    public = os.environ.get('AVIBE_DESKTOP_UPDATER_PUBLIC_KEY', '')
    configuration(enabled, public, os.environ.get('TAURI_SIGNING_PRIVATE_KEY', ''))
    data = json.loads(config.read_text(encoding='utf-8'))
    # Payloads are sealed after final OS signing. We sign those final bytes below.
    data.setdefault('bundle', {})['createUpdaterArtifacts'] = False
    data.setdefault('plugins', {})['updater'] = {'pubkey': public if enabled else '', 'endpoints': []}
    config.write_text(json.dumps(data) + '\n', encoding='utf-8')
    with env_file.open('a', encoding='utf-8') as stream:
        stream.write(f"AVIBE_DESKTOP_UPDATER_PUBLIC_KEY={public if enabled else ''}\n")


def verify(directory: Path, tag: str, source_sha: str, public_key: Path, targets=None) -> None:
    for target in targets or TARGETS:
        subprocess.run([
            'cargo', 'run', '--locked', '--quiet', '--manifest-path', str(ROOT / 'desktop/Cargo.toml'),
            '-p', 'avibe-runtime-host', '--example', 'verify_update', '--',
            str(directory.resolve()), tag, source_sha, target, str(public_key.resolve()),
        ], check=True)


def produce(directory: Path, tag: str, source_sha: str, target: str, app: Path | None) -> None:
    public = os.environ.get('AVIBE_DESKTOP_UPDATER_PUBLIC_KEY', '')
    configuration(True, public, os.environ.get('TAURI_SIGNING_PRIVATE_KEY', ''))
    resolved = release['resolve'](tag, source_sha)
    if release['git_output']('rev-parse', 'HEAD') != source_sha:
        raise ValueError('Updater build checkout differs from release source')
    version = resolved['version']
    provenance = json.loads((directory / f'Avibe_{version}_{target}.SOURCE.json').read_text(encoding='utf-8'))
    if any(provenance.get(k) != v for k, v in {'tag': tag, 'source_sha': source_sha, 'target': target, 'version': version}.items()):
        raise ValueError('Updater source provenance mismatch')
    artifact = directory / f'Avibe_{version}_{target}{TARGETS[target][1]}'
    if target.endswith('apple-darwin'):
        if app is None or not app.is_dir() or app.name != 'Avibe.app':
            raise ValueError('Final sealed Avibe.app is required')
        with tarfile.open(artifact, 'w:gz') as archive:
            archive.add(app, arcname='Avibe.app')
    if not artifact.is_file() or artifact.stat().st_size == 0:
        raise ValueError('Missing updater artifact')
    cli = ROOT / 'desktop/node_modules/.bin' / ('tauri.cmd' if os.name == 'nt' else 'tauri')

    def sign(path):
        # Tauri prints the signature/public key, never pass a secret as argv.
        subprocess.run([str(cli), 'signer', 'sign', str(path.resolve())], check=True, stdout=subprocess.DEVNULL)
        return Path(str(path) + '.sig').read_text(encoding='utf-8').strip()

    signature = sign(artifact)
    manifest = directory / f'desktop-update-{target}.json'
    manifest.write_text(json.dumps({
        'schema_version': 1, 'repository': REPOSITORY, 'tag': tag, 'source_sha': source_sha,
        'channel': 'test' if tag.startswith('gh-v') else 'stable', 'version': version, 'target': target,
        'platforms': {TARGETS[target][0]: {
            'url': f'https://github.com/{REPOSITORY}/releases/download/{tag}/{artifact.name}',
            'signature': signature, 'sha256': release['digest'](artifact), 'size': artifact.stat().st_size,
        }},
    }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    sign(manifest)
    key_file = directory / 'updater-public-key.txt'
    try:
        key_file.write_text(public, encoding='utf-8')
        verify(directory, tag, source_sha, key_file, [target])
    finally:
        key_file.unlink(missing_ok=True)
        Path(str(artifact) + '.sig').unlink(missing_ok=True)


def stage(directory: Path, tag: str, source_sha: str, repo: str, enabled: bool) -> None:
    """Stage verified stable desktop bytes in the existing Draft, read back every byte."""
    if repo != REPOSITORY:
        raise ValueError('Updater repository mismatch')
    paths = release['verify'](directory, tag, source_sha, updater_enabled=enabled)
    state = release['_github']['get_release'](repo, tag)
    if state and not state.draft:
        raise ValueError('Cannot retrofit a published desktop release')
    subprocess.run(['gh', 'release', 'view', tag, '--repo', repo], check=True, stdout=subprocess.DEVNULL)
    # Existing asset bytes are immutable; missing assets may be added only to Drafts.
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
        remote = json.loads(subprocess.check_output(['gh', 'release', 'view', tag, '--repo', repo, '--json', 'assets'], text=True))
        existing = {a['name'] for a in remote['assets']}
        missing = []
        for path in paths:
            if path.name in existing:
                subprocess.run(['gh', 'release', 'download', tag, '--repo', repo, '--pattern', path.name, '--dir', temporary], check=True)
                if release['digest'](Path(temporary) / path.name) != release['digest'](path):
                    raise ValueError('Immutable desktop release asset differs')
            else:
                missing.append(str(path))
        if missing:
            subprocess.run(['gh', 'release', 'upload', tag, '--repo', repo, *missing], check=True)
    release['check_remote'](directory, tag, source_sha, repo, complete=True, updater_enabled=enabled)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    config = sub.add_parser('configure')
    config.add_argument('--config', type=Path, required=True)
    config.add_argument('--env-file', type=Path, required=True)
    config.add_argument('--enabled', action='store_true')
    for action in ('produce', 'stage'):
        command = sub.add_parser(action)
        command.add_argument('--directory', type=Path, required=True)
        command.add_argument('--tag', required=True)
        command.add_argument('--source-sha', required=True)
        if action == 'produce':
            command.add_argument('--target', choices=TARGETS, required=True)
            command.add_argument('--app', type=Path)
        else:
            command.add_argument('--repo', required=True)
            command.add_argument('--enabled', action='store_true')
    args = vars(parser.parse_args())
    globals()[args.pop('command')](**args)


if __name__ == '__main__':
    main()
