"""Prepare the engine signing key. Run it alone: no one else may write this secret meanwhile."""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile

REPO = 'github.com/avibe-bot/avibe'
ENVIRONMENT = 'model-hub-engine-channel-signer'
SECRET = 'MODEL_HUB_ENGINE_CHANNEL_SIGNING_BUNDLE'
TARGET = ['--repo', REPO, '--env', ENVIRONMENT]
UNKNOWN = ('Secret upload outcome unknown. Do not retry or ship this root. Check the signer '
           "environment’s secret name and update time in GitHub Settings before deciding whether to replace it.")


class SetupError(Exception):
    pass


def run(argv, *, env, cwd, data=b'', output=True, failure):
    try:
        result = subprocess.run(argv, input=data, cwd=cwd, env=env, timeout=90, check=True,
                                stdout=subprocess.PIPE if output else subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        raise SetupError(failure) from None
    return result.stdout


def setup(key_id, floor, replace, dry_run):
    gh, avault = shutil.which('gh'), shutil.which('avault')
    if not gh or not avault:
        raise SetupError('Install gh and avault before running setup.')
    # gh needs the owner’s login, not debugging, extensions or alternate endpoints.
    gh_env = {k: os.environ[k] for k in ('HOME', 'GH_CONFIG_DIR', 'XDG_CONFIG_HOME', 'GH_TOKEN', 'GITHUB_TOKEN') if k in os.environ}
    gh_env.update(PATH=os.defpath, GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1')
    with tempfile.TemporaryDirectory(prefix='engine-key-') as home:
        gh_env.update(TMPDIR=home, XDG_CACHE_HOME=home)
        def metadata():
            raw = run([gh, 'secret', 'list', *TARGET, '--json', 'name'], env=gh_env, cwd=home,
                      failure='Cannot read signer environment secret metadata; nothing uploaded.')
            try:
                rows = json.loads(raw)
                if not isinstance(rows, list) or not all(isinstance(r, dict) and isinstance(r.get('name'), str) for r in rows):
                    raise ValueError
            except (ValueError, UnicodeError):
                raise SetupError('Unreadable secret metadata; nothing uploaded.') from None
            if any(r['name'].upper() == SECRET for r in rows) and not replace:
                raise SetupError('Signer bundle already exists; deliberate replacement requires --replace.')

        metadata()
        vault_env = dict(HOME=home, AVAULT_HOME=home, TMPDIR=home, PATH=os.defpath, AVAULT_STORE='file-passphrase')
        unlock = secrets.token_urlsafe(48).encode() + b'\n'
        def vault(args, data=b''):
            raw = run([avault, *args], data=unlock + data, env=vault_env, cwd=home, failure='Throwaway vault operation failed.')
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError
                return value
            except (ValueError, UnicodeError):
                raise SetupError('Invalid response from the throwaway vault.') from None

        key = vault(['keypair', 'generate', '--name', 'MODEL_HUB_ENGINE_CHANNEL', '--curve', 'secp256k1'])
        public = key.get('public_key')
        if not isinstance(public, str) or not re.fullmatch(r'0[23][0-9a-f]{64}', public) or not key.get('envelope'):
            raise SetupError('Throwaway vault returned an invalid signing identity.')
        password = secrets.token_urlsafe(48)
        blob = vault(['key', 'export'], password.encode())
        bundle = json.dumps({'envelope': key['envelope'], 'import': {'passphrase': password, 'blob': blob}}).encode()
        if not dry_run:
            metadata()
            run([gh, 'secret', 'set', SECRET, *TARGET], data=bundle, output=False,
                cwd=home, env=gh_env, failure=UNKNOWN)
    return {'public_key': public, 'key_id': key_id, 'sequence_floor': floor}


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog='Requires a repository administrator with gh logged into github.com (classic repo scope or fine-grained Environments: write; no admin:org).')
    parser.add_argument('--key-id', default='model-hub-channel-2026')
    parser.add_argument('--sequence-floor', type=int, default=1)
    parser.add_argument('--dry-run', action='store_true', help='generate and discard a throwaway key; read metadata but never upload')
    parser.add_argument('--replace', action='store_true', help='permit replacing an existing bundle; use a new key ID for rotation')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.key_id) or args.sequence_floor < 1:
        parser.error('Use a valid channel key ID and a positive sequence floor.')
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    def interrupt(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        print(json.dumps(setup(args.key_id, args.sequence_floor, args.replace, args.dry_run)))
    except SetupError as exc:
        sys.exit(str(exc))
    except (KeyboardInterrupt, OSError):
        sys.exit('Setup interrupted. Check secret metadata before retrying.')


if __name__ == '__main__':
    main()
