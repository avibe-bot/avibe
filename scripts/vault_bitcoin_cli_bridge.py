#!/usr/bin/env python3
"""Internal bridge for vault_bitcoin_regtest.py, never a live-runtime helper.

Only ciphertext and public metadata enter the real Avibe CLI; no daemon starts.
The parent owns the isolated HOME, file-store avault, and known public test keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    root = Path(os.environ['AVIBE_HOME']).resolve().parent
    assert root.name.startswith('taproot-regtest-'), 'Only a regtest-owned temporary directory is allowed'
    assert Path(os.environ['HOME']).resolve() == root / 'home'
    for key in ('HOME', 'AVIBE_HOME', 'AVAULT_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME'):
        assert Path(os.environ[key]).resolve().is_relative_to(root), f'{key} must be test-owned'
    assert os.environ['AVAULT_STORE'] == 'file'
    source = Path(sys.argv[1]).resolve()
    binary = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(source))
    from config.v2_config import V2Config
    from storage import vault_service
    from storage.vault_crypto import Sealed
    from vibe import api

    payload = json.load(sys.stdin)
    config = V2Config.default()
    config.agents.avault.cli_path = str(binary)
    config.save()
    engine = api._vault_engine()
    with engine.begin() as connection:
        try:
            vault_service.get_secret_meta(connection, payload['name'])
        except vault_service.SecretNotFoundError:
            vault_service.create_secret(
                connection, name=payload['name'], sealed=Sealed(**payload['key_envelope']),
                kind='keypair', protection='standard', signer_kind='local',
                public_meta={'signing_public_key': {'curve': 'secp256k1', 'public_key': payload['public_key']}},
            )
    engine.dispose()
    assert api._require_avault_path() == str(binary), 'Signer must be explicit test binary'
    command = [sys.executable, '-m', 'vibe', 'vault', 'sign', payload['name'],
               '--digest', payload['digest'], '--scheme', payload['scheme']]
    result = subprocess.run(command, capture_output=True, text=True, cwd=source, env=os.environ, timeout=40)
    if result.returncode:
        raise RuntimeError(f'Test-owned Avibe CLI failed: {result.stdout}\n{result.stderr}')
    response = json.loads(result.stdout)
    assert response['ok'] is True and response['kind'] == 'vault_signature', response
    assert response['scheme'] == payload['scheme'] and response['digest'] == payload['digest'], response
    json.dump(response['signature'], sys.stdout)


if __name__ == '__main__':
    main()
