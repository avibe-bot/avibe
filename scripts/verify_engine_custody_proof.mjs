// Public verification only. No avault, signing code, network, or caller-selected trust root.
import { createPublicKey, verify } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const keyID = 'model-hub-channel-2026';
const ownerKey = '031ad32248fe2bc3ee60eb0229d67adfc61bfce2f7dbb84ffe47612aa8db62363d';
const statement = 'avibe:model-hub-engine-signer-custody:v1\navibe-bot/avibe\nmodel-hub-channel-2026\nnot a publication\n';

function validSignature(publicKey, message, signature) {
  // SubjectPublicKeyInfo: id-ecPublicKey, secp256k1, compressed SEC1 point.
  const key = createPublicKey({ format: 'der', type: 'spki',
    key: Buffer.from('3036301006072a8648ce3d020106052b8104000a032200' + publicKey, 'hex') });
  return verify('sha256', Buffer.from(message, 'utf8'), key, Buffer.from(signature, 'hex'));
}

function verifyProof(raw) {
  const { signature } = JSON.parse(raw);
  if (typeof signature !== 'string' || !/^(?:[0-9a-f]{2}){8,72}$/.test(signature)) return false;
  // Reconstruct the entire public artifact: no extra bytes, duplicate fields or substituted identity.
  return raw === JSON.stringify({ payload: statement, signature, key_id: keyID }) &&
    validSignature(ownerKey, statement, signature);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    if (process.argv.length !== 3 || !verifyProof(readFileSync(process.argv[2], 'utf8'))) throw new Error();
  } catch {
    console.error('Independent custody verification failed. Stop; do not commission the channel.');
    process.exitCode = 1;
  }
}
