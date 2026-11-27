"""Run the owner CLI in a test-owned home with subprocess doubles, never a real GitHub login."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/model_hub_engine_signing_key.py'
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='owner tool targets macOS')
PUBLIC = '02' + 'ab' * 32
STUB = '''
import hashlib, json, os, pathlib, sys
root = pathlib.Path(__file__).parent
mode = (root/'mode').read_text()
log = root/'calls'
argv = sys.argv[1:]
marker = 'sealed-' + 'sensitive' * 4
secret = 'MODEL_HUB_ENGINE_CHANNEL_SIGNING_BUNDLE'
data = sys.stdin.buffer.read()
vault = pathlib.Path.cwd()
assert vault.parent == root/'tmp' and vault.stat().st_mode & 0o077 == 0
assert marker not in str(argv) and marker not in str(os.environ)
assert 'GH_DEBUG' not in os.environ and 'AVAULT_AGENT_SOCKET' not in os.environ
with log.open('a') as f:
    print(argv[1], file=f)
if pathlib.Path(__file__).name == 'avault':
    assert 'GH_TOKEN' not in os.environ
    unlock, data = data.split(b'\\n',1)
    assert len(unlock) >= 48 and unlock.decode() not in str(argv)+str(os.environ)
    assert os.environ['AVAULT_STORE'] == 'file-passphrase' and all(pathlib.Path(os.environ[k]) == vault for k in ['HOME','AVAULT_HOME','TMPDIR'])
    if argv[1] == 'generate':
        assert argv == ['keypair','generate','--name','MODEL_HUB_ENGINE_CHANNEL','--curve','secp256k1']
        (vault/'machine.key').write_text(marker)
        print(marker,file=sys.stderr)
        print(json.dumps({'public_key':'02'+'ab'*32, 'envelope':marker}))
    else:
        assert argv == ['key','export'] and len(data) >= 48 and data.decode() not in str(argv)+str(os.environ)
        if mode == 'vault-error': sys.exit(marker)
        print(json.dumps({'ciphertext':marker, 'check':hashlib.sha256(data).hexdigest()}))
else:
    assert os.environ['HOME'] == str(root/'daily')
    assert argv[0] == 'secret' and argv[argv.index('--env')+1] == 'model-hub-engine-channel-signer'
    assert argv[argv.index('--repo')+1] == 'github.com/avibe-bot/avibe'
    if argv[1] == 'list':
        second = log.read_text().count('list') == 2
        if mode == 'unreadable' or (mode == 'second-error' and second): sys.exit(marker)
        if mode == 'malformed': print('null'); sys.exit(0)
        print(json.dumps([{'name':secret}] if mode in ('exists','replace') or (mode == 'appeared' and second) else []))
    else:
        assert argv[1:3] == ['set',secret] and 'set' not in log.read_text().splitlines()[:-1]
        b = json.loads(data)
        assert b['envelope'] == b['import']['blob']['ciphertext'] == marker
        pw = b['import']['passphrase']; assert hashlib.sha256(pw.encode()).hexdigest() == b['import']['blob']['check']
        assert pw not in str(argv)+str(os.environ)
        # Check the test-owned filesystem at the handoff, not just after cleanup.
        for p in root.rglob('*'):
            if p.is_file() and vault not in p.parents:
                assert all(s not in p.read_bytes() for s in [marker.encode(),pw.encode()])
        print(marker); print(marker,file=sys.stderr)
        if mode == 'ambiguous': sys.exit(1)
'''


@pytest.mark.parametrize('mode,flags,calls,error', [
    ('fresh', [], 'list generate export list set', None),
    ('dry', ['--dry-run'], 'list generate export', None),
    ('exists', [], 'list', 'already exists'),
    ('replace', ['--replace', '--key-id', 'replacement', '--sequence-floor', '9'], 'list generate export list set', None),
    ('unreadable', ['--replace'], 'list', 'Cannot read'),
    ('malformed', [], 'list', 'Unreadable'),
    ('appeared', [], 'list generate export list', 'already exists'),
    ('second-error', [], 'list generate export list', 'Cannot read'),
    ('vault-error', [], 'list generate export', 'vault operation failed'),
    ('ambiguous', [], 'list generate export list set', 'outcome unknown'),
])
def test_owner_key_boundaries(tmp_path, mode, flags, calls, error):
    for name in ('tmp', 'daily'):
        (tmp_path/name).mkdir()
    for name in ('gh', 'avault'):
        tool = tmp_path/name
        tool.write_text(f'#!{sys.executable}\n' + STUB)
        tool.chmod(0o700)
    (tmp_path/'mode').write_text(mode)
    env = {'PATH': str(tmp_path), 'HOME': str(tmp_path/'daily'), 'TMPDIR': str(tmp_path/'tmp'),
           'GH_DEBUG': 'api', 'GH_TOKEN': 'fixture-token', 'AVAULT_AGENT_SOCKET': 'daily-socket'}
    result = subprocess.run([sys.executable, '-I', SCRIPT, *flags], env=env, cwd=tmp_path, capture_output=True)
    marker = ('sealed-' + 'sensitive' * 4).encode()
    assert marker not in result.stdout + result.stderr
    assert (tmp_path/'calls').read_text().splitlines() == calls.split()
    assert not list((tmp_path/'tmp').iterdir()) and not list((tmp_path/'daily').iterdir())
    assert all(marker not in f.read_bytes() for f in tmp_path.rglob('*') if f.is_file())
    if error:
        assert result.returncode != 0 and not result.stdout and error.encode() in result.stderr
    else:
        assert result.returncode == 0 and not result.stderr
        assert json.loads(result.stdout) == {'public_key': PUBLIC, 'key_id': 'replacement' if mode == 'replace' else 'model-hub-channel-2026',
                                             'sequence_floor': 9 if mode == 'replace' else 1}
