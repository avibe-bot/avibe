"""Public-only fixtures for the one-shot custody proof; no key generation or real signing."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from core import model_hub_engine_channel as channel

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/model-hub-engine-custody-proof.yml'
VERIFY = ROOT / 'scripts/verify_engine_custody_proof.mjs'
STATEMENT = 'avibe:model-hub-engine-signer-custody:v1\navibe-bot/avibe\nmodel-hub-channel-2026\nnot a publication\n'
OWNER = '031ad32248fe2bc3ee60eb0229d67adfc61bfce2f7dbb84ffe47612aa8db62363d'


def test_custody_workflow_has_no_publication_or_caller_selected_input():
    wf = yaml.safe_load(WORKFLOW.read_text())
    assert wf[True] == 'workflow_dispatch' and wf['permissions'] == {'contents': 'read'}
    assert set(wf['jobs']) == {'proof'}
    job = wf['jobs']['proof']
    assert job['if'] == "github.repository == 'avibe-bot/avibe' && github.ref == 'refs/heads/master'"
    assert job['environment'] == 'model-hub-engine-channel-signer'
    steps = job['steps']
    assert '1/1' in steps[0]['run']
    assert [s.get('name') for s in steps if 'secrets.' in json.dumps(s)] == ['Sign the fixed custody statement']
    assert 'verify_engine_custody_proof.mjs' in steps[-2]['run']
    assert steps[-1]['uses'].startswith('actions/upload-artifact@') and steps[-1]['with']['path'] == '${{ runner.temp }}/custody-proof.json'


def test_custody_bytes_are_not_a_channel_payload_or_channel_digest():
    with pytest.raises(channel.ChannelError):
        channel.parse_payload(STATEMENT.encode())
    assert not STATEMENT.encode().startswith(channel.SIGNATURE_DOMAIN)
    assert hashlib.sha256(STATEMENT.encode()).hexdigest() != channel.channel_digest(STATEMENT.encode())


@pytest.mark.skipif(os.name != 'posix', reason='the hosted proof runs on Linux')
def test_signer_handoff_uses_only_fixed_digest_and_emits_only_public_fields(tmp_path):
    binary = tmp_path / 'avault/avault'
    binary.parent.mkdir()
    digest = hashlib.sha256(STATEMENT.encode()).hexdigest()
    binary.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
assert 'CHANNEL_SIGNER_BUNDLE' not in os.environ
assert Path.cwd() == Path(os.environ['AVAULT_HOME']) and Path.cwd().parent == Path({str(tmp_path)!r})
d = json.load(sys.stdin)
if sys.argv[1:] == ['key', 'import']:
    assert d == {{'fixture': 'import-only-marker'}}
else:
    assert sys.argv[1:] == ['sign']
    assert d == {{'digest': {digest!r}, 'key_envelope': {{'fixture': 'wrapped-only-marker'}}, 'scheme': 'ecdsa-secp256k1-der', 'name': 'MODEL_HUB_ENGINE_CHANNEL'}}
    print(json.dumps({{'signature': 'aa'*70}}))
print('stub output not for logs', file=sys.stderr)
''')
    binary.chmod(0o700)
    env = dict(GITHUB_REPOSITORY='avibe-bot/avibe', GITHUB_REF='refs/heads/master', GITHUB_RUN_ATTEMPT='1',
               GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_RUN_NUMBER='1', RUNNER_TEMP=str(tmp_path),
               GITHUB_WORKFLOW_REF=f'avibe-bot/avibe/.github/workflows/{WORKFLOW.name}@refs/heads/master',
               CHANNEL_SIGNER_BUNDLE=json.dumps({'import': {'fixture': 'import-only-marker'}, 'envelope': {'fixture': 'wrapped-only-marker'}}))
    result = subprocess.run([sys.executable, '-I', ROOT / 'scripts/model_hub_engine_custody_proof.py'], env=env, capture_output=True)
    assert result.returncode == 0 and not result.stdout and not result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ['avault', 'custody-proof.json']
    assert json.loads((tmp_path / 'custody-proof.json').read_text()) == {
        'key_id': 'model-hub-channel-2026', 'payload': STATEMENT, 'signature': 'aa' * 70}


def test_public_verifier_positive_vector_and_refusals(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('public verifier requires Node.js')
    vectors = json.loads((ROOT / 'tests/fixtures/avault/verify_ecdsa_secp256k1_der.json').read_text())['cases']
    vector = next(v for v in vectors if 'digest_preimage' in v)
    message = ''.join(vector['digest_preimage'][k] for k in ('domain_utf8', 'payload_utf8'))
    # Change only the two public pins in a test copy to use an existing known-answer vector.
    # The actual verifier must not accept these bytes, this key, or this signature.
    fixture = tmp_path / 'fixture.mjs'
    fixture.write_text(VERIFY.read_text().replace(OWNER, vector['public_key_hex']).replace(
        json.dumps(STATEMENT)[1:-1], message.replace('\\', '\\\\').replace('\n', '\\n')))
    proof = {'payload': message, 'signature': vector['signature_hex'], 'key_id': 'model-hub-channel-2026'}
    path = tmp_path / 'proof.json'
    def run(script, envelope):
        path.write_text(json.dumps(envelope, separators=(',', ':')))
        return subprocess.run([node, script, path], capture_output=True).returncode
    assert run(fixture, proof) == 0
    for change in ({'payload': message + 'x'}, {'signature': '00' * 70}, {'key_id': 'other'}, {'extra': 'unexpected'}):
        assert run(fixture, {**proof, **change}) != 0
    assert run(VERIFY, {**proof, 'payload': STATEMENT}) != 0
