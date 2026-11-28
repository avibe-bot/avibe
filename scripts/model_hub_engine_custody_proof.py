"""One custody statement, not a channel publication. No inputs select the signed bytes."""
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile

STATEMENT = 'avibe:model-hub-engine-signer-custody:v1\navibe-bot/avibe\nmodel-hub-channel-2026\nnot a publication\n'


def main():
    if not all(os.environ.get(k) == v for k, v in {
        'GITHUB_REPOSITORY': 'avibe-bot/avibe', 'GITHUB_REF': 'refs/heads/master',
        'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_RUN_NUMBER': '1',
        'GITHUB_WORKFLOW_REF': 'avibe-bot/avibe/.github/workflows/model-hub-engine-custody-proof.yml@refs/heads/master',
    }.items()):
        raise ValueError
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    temp = Path(os.environ['RUNNER_TEMP'])
    bundle = json.loads(os.environ.pop('CHANNEL_SIGNER_BUNDLE'))
    with tempfile.TemporaryDirectory(dir=temp) as home:
        def call(args, data):
            return subprocess.run([temp / 'avault/avault', *args], input=json.dumps(data).encode(),
                                  capture_output=True, check=True, timeout=30, cwd=home,
                                  env={'HOME': home, 'TMPDIR': home, 'AVAULT_HOME': home, 'AVAULT_STORE': 'file', 'PATH': os.defpath}).stdout
        call(['key', 'import'], bundle['import'])
        signed = call(['sign'], {'name': 'MODEL_HUB_ENGINE_CHANNEL', 'key_envelope': bundle['envelope'],
                                 'scheme': 'ecdsa-secp256k1-der', 'digest': hashlib.sha256(STATEMENT.encode()).hexdigest()})
        signature = json.loads(signed)['signature']
        if not isinstance(signature, str) or not re.fullmatch(r'(?:[0-9a-f]{2}){8,72}', signature):
            raise ValueError
    envelope = {'payload': STATEMENT, 'signature': signature, 'key_id': 'model-hub-channel-2026'}
    (temp / 'custody-proof.json').write_text(json.dumps(envelope, separators=(',', ':')), encoding='ascii')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never include the bundle, subprocess output or a parse exception in Actions logs.
        raise SystemExit('Custody proof failed; stop before commissioning.') from None
